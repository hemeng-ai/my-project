"""
orchestrator.py — 负责"找哪个 agent"

接收用户任务 → 判断路由 → 按顺序调用 agent 流水线 → 返回最终结果。

规范流水线（与 CLAUDE.md、base_agent.py AGENT_MODEL_MAP 保持一致，
1 个编排器 + 6 个专业子 agent，共 7 个）：
    main-orchestrator
      → test-agent（TDD，先写失败测试，Flash）
      → coding-agent（最简实现，Flash）
      → review-agent（Pro，只读，两阶段：spec 合规 + 代码质量）
      → security-agent（Pro，只读，仅当涉及认证/输入/敏感数据/外部 API 时）
      → fix-agent（Flash，最小修复，最多 2 轮，修完重新过 review）
      → docs-agent（Flash，仅在审查全部通过后）

已删除 reflect-agent：需求一致性验证并入 review-agent 的 spec 合规
审查，不再单独设一个 agent；原来 is_reflect_passed 的关键词判断
已删除，改为结构化 verdict（见 parse_verdict）。

重要：run_agent 返回的 success 只表示 API 调用成功，不表示质量
通过。review / security 的 FAIL 会阻塞流水线，不会进入 docs-agent。

P1 新增：真实门禁（gates.py：跑测试 / lint / git diff）、检查点与
断点续跑（.pipeline-state.json）、JSONL 运行日志、任务队列
（run_queue）、WIP 上限。单任务内的 test → coding → review 串行；
并行只发生在不同任务之间，且 coding / review 同一时刻只做一个。
"""

import json
import re
import sys
import time
from pathlib import Path

import gates
from base_agent import get_cost_summary, run_agent

# ── 流水线定义 ────────────────────────────────────────────
# 规范顺序。security-agent 和 fix-agent 是条件步骤（见 run_pipeline），
# 不在每次运行中都无条件执行，但顺序固定：review → security → fix → docs。
PIPELINE = [
    "main-orchestrator",   # 1. 拆解任务，生成执行计划
    "test-agent",          # 2. TDD：先写失败测试
    "coding-agent",        # 3. 实现代码
    "review-agent",        # 4. 两阶段审查（spec 合规 + 代码质量，只读）
    "security-agent",      # 5. 安全检查（只读，仅当触发条件满足时）
    "fix-agent",           # 6. 最小修复（最多 2 轮，修完重新过 review）
    "docs-agent",          # 7. 生成文档（仅在审查全部通过后）
]

# 修复循环最大次数（与 CLAUDE.md 一致）
MAX_FIX_ROUNDS = 2

# security-agent 的触发条件（P2：单一事实来源，与 CLAUDE.md、
# security-agent.md 的触发表保持一致）。
# 键是触发类别，值是任务文本 / 变更文件名里的关键词。
# 关键词初筛只是回退判断；最终是否需要安全审查，以 review-agent
# 结构化 verdict 中的 needs_security_review 为准。
SECURITY_TRIGGERS = {
    "auth": ["认证", "登录", "登出", "密码", "token", "secret", "密钥",
             "auth", "login", "logout", "password", "session", "jwt", "mfa", "权限"],
    "input": ["用户输入", "输入校验", "输入处理", "文件上传", "api 参数",
              "input", "upload", "validation", "sanitiz"],
    "sensitive_data": ["敏感数据", "隐私", "个人信息", "支付", "payment",
                       "credit card", "ssn", "身份证"],
    "external_api": ["外部 api", "外部api", "第三方", "webhook",
                     "external api", "third-party", "thirdparty"],
}
# 兼容旧名：扁平关键词列表（由 SECURITY_TRIGGERS 派生，不要手改）。
SECURITY_TRIGGER_KEYWORDS = sorted({kw for kws in SECURITY_TRIGGERS.values() for kw in kws})

# 任务队列的 WIP 上限（P1）：同一时刻每个 agent 最多同时做几个任务。
# 队内任务按序跑，一个任务的 test → coding → review 串行；并行只发生
# 在不同任务之间，且 coding / review 同一时刻只做一个。
WIP_LIMITS = {
    "main-orchestrator": 1,
    "test-agent": 2,
    "coding-agent": 1,
    "review-agent": 1,
    "security-agent": 1,
    "fix-agent": 2,
    "docs-agent": 1,
}

# 持久化文件（P1）：默认放在仓库根目录（运行 orchestrator.py 的 cwd），
# 属于本地运行状态，不要提交（已加入 .gitignore）。
DEFAULT_STATE_PATH = ".pipeline-state.json"
DEFAULT_LOG_PATH = "pipeline-run.log.jsonl"

VERDICT_JSON_INSTRUCTIONS = (
    "请在回复末尾附上一段 JSON 审查裁决（不要只写自然语言结论），格式如下：\n"
    '```json\n'
    '{"verdict": "pass" | "fail", "critical": 0, "warning": 0, '
    '"needs_security_review": false, "issues": ["问题描述", "..."]}\n'
    '```\n'
    "verdict 为 pass 表示无阻塞问题；critical > 0 时 verdict 必须为 fail。"
)


# ── 工具函数 ──────────────────────────────────────────────
def print_stage(stage: str, agent: str, model: str):
    print(f"\n{'='*50}")
    print(f"▶ {stage}")
    print(f"  Agent: {agent}  |  Model: {model}")
    print('='*50)


def print_result(result: dict):
    if result["success"]:
        print(result["response"])
    else:
        print(f"[错误] {result['error']}")


def _extract_verdict_json(response: str):
    """从回复文本中提取第一个带 verdict 键的 JSON 对象；找不到返回 None。"""
    if not response:
        return None
    candidates = []
    # 1) ```json ... ``` 代码块优先
    for block in re.findall(r"```json\s*(.*?)\s*```", response, re.DOTALL | re.IGNORECASE):
        candidates.append(block)
    # 2) 其余 {...} 片段（按出现顺序）
    for block in re.findall(r"\{[^{}]*\"verdict\"[^{}]*\}", response, re.DOTALL):
        candidates.append(block)
    for raw in candidates:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "verdict" in data:
            return data
    return None


def parse_verdict(response: str) -> dict:
    """解析审查裁决。

    输入 review-agent / security-agent 的回复文本，返回结构化裁决：
        {
            "verdict": "pass" | "fail",
            "critical": int, "warning": int,
            "needs_security_review": bool,
            "issues": [str, ...],
            "parse_error": str | None,
        }

    安全规则（刻意保守）：
    - 找不到可解析的 JSON verdict，或 verdict 缺失/不在 pass/fail 内 → FAIL。
    - critical > 0 → 强制 FAIL（严重问题阻塞合并，与 review-agent 的
      CRITICAL 定义一致），即使模型写了 pass 也不放行。
    - 永远不默认通过。没有可信的 pass 证据，就按 FAIL 处理，需人工复核。
    """
    data = _extract_verdict_json(response or "")
    if data is None:
        return {
            "verdict": "fail",
            "critical": 0,
            "warning": 0,
            "needs_security_review": False,
            "issues": ["未返回结构化审查裁决（缺少可解析的 JSON verdict），按 FAIL 处理，需人工复核"],
            "parse_error": "missing or invalid JSON verdict",
        }

    raw_verdict = str(data.get("verdict", "")).strip().lower()
    verdict_map = {
        "pass": "pass", "通过": "pass",
        "fail": "fail", "不通过": "fail", "失败": "fail",
    }
    verdict = verdict_map.get(raw_verdict)
    if verdict is None:
        return {
            "verdict": "fail",
            "critical": 0,
            "warning": 0,
            "needs_security_review": bool(data.get("needs_security_review", False)),
            "issues": [f"审查裁决 verdict 值无法识别：{data.get('verdict')!r}，按 FAIL 处理，需人工复核"],
            "parse_error": f"unrecognized verdict value: {data.get('verdict')!r}",
        }

    try:
        critical = int(data.get("critical", 0) or 0)
    except (TypeError, ValueError):
        critical = 0
    try:
        warning = int(data.get("warning", 0) or 0)
    except (TypeError, ValueError):
        warning = 0
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]

    # CRITICAL > 0 时强制 FAIL，即使模型写了 pass。
    if critical > 0 and verdict == "pass":
        verdict = "fail"
        issues = issues + ["critical > 0，强制判定为 FAIL（严重问题阻塞合并）"]

    return {
        "verdict": verdict,
        "critical": critical,
        "warning": warning,
        "needs_security_review": bool(data.get("needs_security_review", False)),
        "issues": [str(i) for i in issues],
        "parse_error": None,
    }


def _needs_security_review(user_task, review_verdict, changed_files=None):
    """判断是否需要跑 security-agent。返回 (是否需要, [触发类别])。

    优先采信 review-agent 裁决中的 needs_security_review；
    否则回退到对任务文本 + 变更文件名的 SECURITY_TRIGGERS 关键词初筛。
    这只是触发判断，不是质量裁决。
    """
    if review_verdict.get("needs_security_review"):
        return True, ["review-verdict"]
    haystacks = [(user_task or "").lower()]
    for f in (changed_files or []):
        haystacks.append(str(f).lower())
    matched = []
    for category, keywords in SECURITY_TRIGGERS.items():
        if any(kw.lower() in text for text in haystacks for kw in keywords):
            matched.append(category)
    return bool(matched), matched


def _log_stage(log_path, run_id, stage, agent, model, verdict, duration_s, extra=None):
    """往 JSONL 运行日志追加一行。只记元数据，不记密钥或正文全文。"""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": run_id,
        "stage": stage,
        "agent": agent,
        "model": model,
        "verdict": verdict,
        "duration_s": round(duration_s, 2),
    }
    if extra:
        record.update(extra)
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[日志] 写运行日志失败（不阻塞流水线）：{exc}")


def _save_checkpoint(state_path, data):
    try:
        Path(state_path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[检查点] 写检查点失败（不阻塞流水线）：{exc}")


def _load_checkpoint(state_path):
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _human_intervention(results: dict, reason: str) -> dict:
    return {
        "success": False,
        "results": results,
        "final_output": None,
        "human_intervention_required": True,
        "human_intervention_reason": reason,
    }


def _api_failure(results: dict, agent_name: str, result: dict) -> dict:
    return {
        "success": False,
        "results": results,
        "final_output": None,
        "human_intervention_required": False,
        "human_intervention_reason": f"{agent_name} 执行失败（API 调用失败）：{result.get('error')}",
        "baseline_sha": None,
        "cost_summary": get_cost_summary(),
    }


# ── 主调度函数 ────────────────────────────────────────────
def run_pipeline(user_task: str, resume: bool = False, cwd: str = ".",
                 state_path: str = None, log_path: str = None) -> dict:
    """
    运行完整的开发流水线。

    参数：
        user_task   用户的原始任务描述
        resume      True 时从检查点恢复（同一任务、同一 state 文件），
                    跳过已完成的阶段，而不是从头重跑
        cwd         运行测试 / git 命令的工作目录（默认当前目录）
        state_path  检查点文件路径（默认 .pipeline-state.json）
        log_path    JSONL 运行日志路径（默认 pipeline-run.log.jsonl）

    返回：
        {
            "success": True/False,
            "results": 每个 agent 的输出记录,
            "final_output": 最终结果（docs-agent 的输出）,
            "human_intervention_required": True/False,
            "human_intervention_reason": 原因（如需人工介入）
            "baseline_sha": coding 前记录的 git HEAD（用于回滚，见 P2.7）,
            "cost_summary": 用量 / 成本汇总（见 base_agent.COST_TRACKER）,
        }

    门禁规则（P1，真实执行，不是只靠 LLM 自述）：
    - test-agent 之后必须跑测试：新测试必须失败，且失败原因必须是
      “未实现”（断言 / NotImplementedError），语法或环境错误会直接
      触发人工介入；新测试全部通过（红灯缺失）同样触发人工介入。
    - coding-agent 之后必须跑测试：测试必须全过，否则进 fix 循环。
    - fix-agent 之后重新跑测试：新出现的失败（回归）会记入下一轮
      修复输入；轮次用满仍失败 → 人工介入，并在原因里附 baseline SHA。
    - lint 是 best-effort：配置了 linter 且失败时记入日志并追加到
      审查上下文，不单独硬拦（避免仓库既有 lint 问题卡死流水线）。
    - 任何门禁失败都不进入 docs-agent。
    """
    state_path = state_path or DEFAULT_STATE_PATH
    log_path = log_path or DEFAULT_LOG_PATH
    run_id = f"{int(time.time())}"
    print(f"\n{'#'*50}")
    print(f"# 任务开始")
    print(f"# {user_task[:80]}{'...' if len(user_task) > 80 else ''}")
    print(f"{'#'*50}")

    results = {}
    previous_output = None
    completed = set()
    fix_rounds_used = 0
    baseline = {"sha": None, "test_status": None, "failed_tests": []}

    if resume:
        ckpt = _load_checkpoint(state_path)
        if ckpt and ckpt.get("task") == user_task:
            results = ckpt.get("results", {})
            previous_output = ckpt.get("previous_output")
            completed = set(ckpt.get("completed", []))
            fix_rounds_used = int(ckpt.get("fix_rounds_used", 0))
            baseline = ckpt.get("baseline", baseline)
            print(f"[恢复] 从检查点恢复，已完成阶段：{sorted(completed)}，"
                  f"已用修复轮次：{fix_rounds_used}，baseline：{baseline.get('sha')}")
        else:
            print("[恢复] 没有可用的同任务检查点，从头开始")

    def checkpoint():
        _save_checkpoint(state_path, {
            "task": user_task,
            "run_id": run_id,
            "completed": sorted(completed),
            "fix_rounds_used": fix_rounds_used,
            "baseline": baseline,
            "previous_output": previous_output,
            "results": results,
        })

    def run_stage(agent_name: str, message: str, extra_context=None):
        """跑一个 agent；API 失败返回 failure dict，成功返回 result。"""
        import base_agent
        model = base_agent.AGENT_MODEL_MAP.get(agent_name, "deepseek-chat")
        print_stage(f"阶段：{agent_name}", agent_name, model)
        started = time.time()
        result = run_agent(
            agent_name=agent_name,
            user_message=message,
            extra_context=extra_context,
        )
        _log_stage(log_path, run_id, agent_name, agent_name, model,
                   "ok" if result["success"] else "api-error",
                   time.time() - started,
                   {"cost_summary": get_cost_summary()})
        print_result(result)
        return result

    def gate_tests(label):
        """跑一次测试门禁，返回 gates.run_tests 的结果字典。"""
        started = time.time()
        test_result = gates.run_tests(cwd=cwd)
        _log_stage(log_path, run_id, f"gate:{label}", "gates", "test",
                   test_result["status"], time.time() - started,
                   {"runner": test_result.get("runner"),
                    "failure_kind": test_result.get("failure_kind"),
                    "failed_tests": test_result.get("failed_tests")})
        print(f"\n门禁[{label}]：{test_result['status']} "
              f"(runner={test_result.get('runner')}, "
              f"failure_kind={test_result.get('failure_kind')})")
        return test_result

    def fail_with_baseline(reason):
        out = _human_intervention(results, reason)
        if baseline.get("sha"):
            out["human_intervention_reason"] += (
                f"\n\n基线（coding 前的 git HEAD，可用于回滚）：{baseline['sha']}\n"
                f"回滚方法：git stash 或 git checkout {baseline['sha']} -- <相关文件>，"
                "确认测试恢复后再重新派发任务。不要无确认执行 git reset --hard。")
        out["baseline_sha"] = baseline.get("sha")
        out["cost_summary"] = get_cost_summary()
        return out

    # ── 阶段 1-3：编排 → TDD 测试 → 实现 ──────────────────
    for agent_name in ["main-orchestrator", "test-agent", "coding-agent"]:
        if agent_name in completed:
            print(f"\n[恢复] 跳过已完成阶段：{agent_name}")
            continue
        result = run_stage(agent_name, user_task, extra_context=previous_output)
        results[agent_name] = result
        if not result["success"]:
            print(f"\n❌ {agent_name} 执行失败，流水线中断")
            return _api_failure(results, agent_name, result)
        previous_output = result["response"]
        completed.add(agent_name)
        checkpoint()

        if agent_name == "test-agent":
            # 红灯门禁：新测试必须失败，且只能是“未实现”类失败。
            test_result = gate_tests("red-light")
            if test_result["status"] == "skipped":
                print("[门禁] 未检测到测试框架，红灯校验跳过（已记录）")
            elif test_result["status"] == "pass":
                return fail_with_baseline(
                    "红灯门禁失败：test-agent 写完后测试全部通过，"
                    "没有出现预期的失败测试（可能实现已存在，或测试是空断言）。")
            elif test_result["failure_kind"] in ("syntax", "environment"):
                return fail_with_baseline(
                    "红灯门禁失败：测试失败原因是 "
                    f"{test_result['failure_kind']}，不是“未实现”。"
                    "请检查测试文件或环境，不要派 fix-agent 改测试。"
                    f"\n测试输出尾部：\n{test_result['output_tail'][-1500:]}")
            baseline["failed_tests"] = test_result.get("failed_tests") or []
            baseline["test_status"] = "red-light-ok"
            checkpoint()

        if agent_name == "coding-agent":
            # 基线（P2.7）：coding 前的 git HEAD + 变更摘要，写进检查点。
            baseline["sha"] = gates.get_git_head(cwd=cwd)
            baseline["diff_stat"] = gates.get_git_diff_stat(cwd=cwd)
            checkpoint()
            # 绿灯门禁：测试必须全过。
            test_result = gate_tests("green-light")
            if test_result["status"] == "skipped":
                print("[门禁] 未检测到测试框架，绿灯校验跳过（已记录）")
            elif test_result["status"] != "pass":
                # 测试失败 → fix 循环（与审查修复共用轮次上限）。
                while fix_rounds_used < MAX_FIX_ROUNDS:
                    fix_rounds_used += 1
                    print(f"\n⚠️  实现后测试未通过，进入 fix-agent 第 {fix_rounds_used} 轮修复...")
                    fix_result = run_stage(
                        "fix-agent", user_task,
                        extra_context=(
                            "测试未通过（请针对性做最小修复，不要扩大范围）：\n"
                            f"失败类型：{test_result.get('failure_kind')}\n"
                            f"失败用例：{test_result.get('failed_tests')}\n"
                            f"测试输出尾部：\n{test_result['output_tail'][-1500:]}"
                        ),
                    )
                    results[f"fix-agent-round{fix_rounds_used}"] = fix_result
                    if not fix_result["success"]:
                        print("\n❌ fix-agent 执行失败，流水线中断")
                        return _api_failure(results, "fix-agent", fix_result)
                    previous_output = fix_result["response"]
                    checkpoint()
                    test_result = gate_tests(f"green-light-after-fix-{fix_rounds_used}")
                    if test_result["status"] == "pass":
                        break
                if test_result["status"] != "pass":
                    return fail_with_baseline(
                        f"绿灯门禁失败：coding 后测试仍未通过（已用满 {MAX_FIX_ROUNDS} 轮修复）。"
                        f"\n失败类型：{test_result.get('failure_kind')}，"
                        f"失败用例：{test_result.get('failed_tests')}")
            # lint 是 best-effort：失败只记日志并追加到审查上下文，不单独硬拦。
            lint_result = gates.run_lint(cwd=cwd)
            print(f"\nLint：{lint_result['status']} (tool={lint_result.get('tool')})")
            if lint_result["status"] == "fail":
                previous_output = (previous_output or "") + (
                    "\n\n[lint 警告] lint 未通过，请在审查时一并处理：\n"
                    f"{lint_result['output_tail'][-1500:]}")

    # ── 阶段 4-6：审查 → 安全（按需）→ 修复循环 ───────────
    last_verdict = None
    last_review_response = ""

    while True:
        # 4a. review-agent（Pro，只读，两阶段审查），要求结构化裁决。
        review_result = run_stage(
            "review-agent",
            user_task + "\n\n" + VERDICT_JSON_INSTRUCTIONS,
            extra_context=previous_output,
        )
        review_key = "review-agent" if "review-agent" not in results else f"review-agent-round{fix_rounds_used + 1}"
        results[review_key] = review_result
        if not review_result["success"]:
            print("\n❌ review-agent 执行失败，流水线中断")
            return _api_failure(results, "review-agent", review_result)

        last_review_response = review_result["response"] or ""
        last_verdict = parse_verdict(last_review_response)
        print(f"\n审查裁决：{last_verdict['verdict']} "
              f"(critical={last_verdict['critical']}, warning={last_verdict['warning']})")
        previous_output = last_review_response

        # 4b. security-agent（Pro，只读，仅当触发条件满足时）。
        security_verdict = None
        changed = gates.get_git_diff_stat(cwd=cwd).get("changed_files") or []
        need_security, categories = _needs_security_review(user_task, last_verdict, changed)
        if need_security:
            print(f"\n安全审查：触发（类别：{categories}）")
            security_result = run_stage(
                "security-agent",
                user_task + "\n\n" + VERDICT_JSON_INSTRUCTIONS,
                extra_context=previous_output,
            )
            results["security-agent"] = security_result
            if not security_result["success"]:
                print("\n❌ security-agent 执行失败，流水线中断")
                return _api_failure(results, "security-agent", security_result)
            security_verdict = parse_verdict(security_result["response"] or "")
            print(f"\n安全裁决：{security_verdict['verdict']} "
                  f"(critical={security_verdict['critical']}, warning={security_verdict['warning']})")
            previous_output = security_result["response"]
            if security_verdict["verdict"] == "fail":
                last_verdict = security_verdict
                last_review_response = security_result["response"] or ""
        else:
            results["security-agent"] = {
                "agent": "security-agent",
                "model": None,
                "response": None,
                "success": True,
                "skipped": True,
                "reason": "未触发安全审查条件（不涉及认证/输入/敏感数据/外部 API）",
            }
            print("\n安全审查：跳过（未触发条件）")

        # 4c. 裁决通过 → 进入文档阶段；不通过 → 修复或人工介入。
        if last_verdict["verdict"] == "pass":
            print("\n✅ 审查全部通过，进入文档阶段")
            completed.add("review-agent")
            checkpoint()
            break

        if fix_rounds_used >= MAX_FIX_ROUNDS:
            print(f"\n🚨 fix-agent 已用满 {MAX_FIX_ROUNDS} 轮仍不通过，需要人工介入")
            return fail_with_baseline(
                f"review/security 经过 {MAX_FIX_ROUNDS} 轮修复后仍不通过。\n"
                f"最后一次裁决：{last_verdict}\n\n"
                f"请判断：\n"
                f"  A. 代码方向跑偏 → 否决，重新描述需求\n"
                f"  B. 审查判断有误 → 放行，修正 review-agent.md\n"
                f"  C. 需求描述不清晰 → 重新澄清需求后重启\n\n"
                f"最后一次审查报告：\n{last_review_response}",
            )

        fix_rounds_used += 1
        print(f"\n⚠️  审查不通过，进入 fix-agent 第 {fix_rounds_used} 轮修复...")
        pre_fix_tests = gate_tests(f"pre-fix-{fix_rounds_used}")
        fix_result = run_stage(
            "fix-agent",
            user_task,
            extra_context=(
                f"审查反馈（请针对性做最小修复，不要扩大范围）：\n"
                f"裁决：{last_verdict}\n\n{last_review_response}"
            ),
        )
        results[f"fix-agent-round{fix_rounds_used}"] = fix_result
        if not fix_result["success"]:
            print("\n❌ fix-agent 执行失败，流水线中断")
            return _api_failure(results, "fix-agent", fix_result)
        previous_output = fix_result["response"]
        checkpoint()
        # 回归门禁：修复不能引入新的失败（与修复前相比，排除 TDD 新测试）。
        post_fix_tests = gate_tests(f"post-fix-{fix_rounds_used}")
        if post_fix_tests["status"] == "fail":
            before = set(pre_fix_tests.get("failed_tests") or [])
            after = set(post_fix_tests.get("failed_tests") or [])
            baseline_failed = set(baseline.get("failed_tests") or [])
            new_failures = after - before - baseline_failed
            if new_failures:
                print(f"\n⚠️  回归门禁：修复引入了新的失败：{sorted(new_failures)}，"
                      "将作为下一轮修复的输入")
                previous_output = (previous_output or "") + (
                    "\n\n[回归门禁] 本轮修复引入了新的测试失败，请优先处理：\n"
                    f"{sorted(new_failures)}")
        # 循环回到 review-agent 重新审查（不直接进入 docs-agent）。

    # ── 阶段 7：文档（仅在审查全部通过后）─────────────────
    docs_result = run_stage("docs-agent", user_task, extra_context=previous_output)
    results["docs-agent"] = docs_result
    if not docs_result["success"]:
        print("\n❌ docs-agent 执行失败，流水线中断")
        return _api_failure(results, "docs-agent", docs_result)
    completed.add("docs-agent")
    checkpoint()

    # ── 全部通过 ──────────────────────────────────────────
    print(f"\n{'#'*50}")
    print("# ✅ 流水线全部完成")
    print(f"{'#'*50}\n")

    return {
        "success": True,
        "results": results,
        "final_output": results.get("docs-agent", {}).get("response"),
        "human_intervention_required": False,
        "human_intervention_reason": None,
        "baseline_sha": baseline.get("sha"),
        "cost_summary": get_cost_summary(),
    }


def run_queue(tasks_file: str, cwd: str = ".") -> dict:
    """任务队列（P1）：按序跑多个独立任务，一个失败不中断整队。

    tasks_file 是 JSONL：每行一个任务，形如
        {"title": "登录页加验证码", "task": "……"}
    或只有 {"task": "……"}；纯文本行也当作任务描述。
    每个任务用独立的检查点/日志文件（tasks_file + ".{i}.state.json"），
    避免互相覆盖。同一时刻只有一个任务在跑（coding / review 的
    WIP 上限都是 1，见 WIP_LIMITS），并行只体现在“任务之间按序
    推进、互不阻塞”，不做多线程并行执行——单任务内的
    test → coding → review 必须串行，多线程并行会打乱 git 状态
    和检查点，这一点在 PR 说明里如实写明。
    """
    tasks = []
    with open(tasks_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict) and item.get("task"):
                    tasks.append(item)
                elif isinstance(item, str):
                    tasks.append({"task": item})
            except json.JSONDecodeError:
                tasks.append({"task": line})
    summary = {"total": len(tasks), "succeeded": 0, "failed": 0, "results": []}
    results_path = f"{tasks_file}.results.jsonl"
    for i, item in enumerate(tasks, start=1):
        title = item.get("title") or item["task"][:60]
        print(f"\n{'='*50}\n队列任务 {i}/{len(tasks)}：{title}\n{'='*50}")
        result = run_pipeline(
            item["task"],
            cwd=cwd,
            state_path=f"{tasks_file}.{i}.state.json",
            log_path=f"{tasks_file}.{i}.run.log.jsonl",
        )
        entry = {"index": i, "title": title,
                 "success": bool(result.get("success")),
                 "human_intervention_required": bool(result.get("human_intervention_required")),
                 "reason": result.get("human_intervention_reason"),
                 "baseline_sha": result.get("baseline_sha"),
                 "cost_summary": result.get("cost_summary")}
        summary["results"].append(entry)
        if entry["success"]:
            summary["succeeded"] += 1
        else:
            summary["failed"] += 1
        try:
            with open(results_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[队列] 写结果文件失败：{exc}")
    print(f"\n队列完成：共 {summary['total']} 个，成功 {summary['succeeded']} 个，"
          f"失败 {summary['failed']} 个。明细见 {results_path}")
    return summary


# ── 单独调用某个 agent（调试用）──────────────────────────
def run_single(agent_name: str, message: str):
    """直接调用某一个 agent，不走完整流水线，用于调试。"""
    import base_agent
    model = base_agent.AGENT_MODEL_MAP.get(agent_name, "未知")
    print_stage(f"单独调试：{agent_name}", agent_name, model)
    result = run_agent(agent_name=agent_name, user_message=message)
    print_result(result)
    return result


# ── 入口 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="多智能体开发流水线")
    parser.add_argument("task", nargs="?", help="任务描述")
    parser.add_argument("--resume", action="store_true", help="从检查点恢复同一任务")
    parser.add_argument("--cwd", default=".", help="运行测试/git 命令的工作目录")
    parser.add_argument("--state", default=None, help="检查点文件路径")
    parser.add_argument("--log", default=None, help="JSONL 运行日志路径")
    parser.add_argument("command", nargs="?", help="queue 或 debug")
    parser.add_argument("arg2", nargs="?", help="queue 时为 tasks 文件，debug 时为 agent 名称")
    parser.add_argument("arg3", nargs="?", help="debug 时为消息")
    args = parser.parse_args()

    if args.command == "queue" and args.arg2:
        summary = run_queue(args.arg2, cwd=args.cwd)
        sys.exit(0 if summary["failed"] == 0 else 1)

    if (args.task == "debug" or args.command == "debug") and args.arg2:
        agent_name = args.arg2
        message = args.arg3 or args.task or ""
        run_single(agent_name=agent_name, message=message)
        sys.exit(0)

    if not args.task:
        parser.print_help()
        print("\n用法：")
        print("  完整流水线：python orchestrator.py '你的任务描述'")
        print("  断点续跑：  python orchestrator.py --resume '你的任务描述'")
        print("  任务队列：  python orchestrator.py queue tasks.jsonl")
        print("  单独调试：  python orchestrator.py debug <agent名称> '消息'")
        sys.exit(1)

    result = run_pipeline(args.task, resume=args.resume, cwd=args.cwd,
                          state_path=args.state, log_path=args.log)

    if result["human_intervention_required"]:
        print("\n" + "!" * 50)
        print("需要人工介入：")
        print(result["human_intervention_reason"])
        print("!" * 50)
