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
"""

import json
import re
from base_agent import run_agent

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

# security-agent 的触发关键词（启发式初筛；最终是否需要安全审查，
# 以 review-agent 的结构化 verdict 中 needs_security_review 为准，
# 关键词只是 verdict 缺失时的回退判断）
SECURITY_TRIGGER_KEYWORDS = [
    "认证", "登录", "登出", "密码", "token", "secret", "密钥",
    "auth", "login", "password", "session", "jwt",
    "用户输入", "输入校验", "敏感数据", "隐私",
    "外部 api", "外部api", "第三方", "支付", "payment",
]

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


def _needs_security_review(user_task: str, review_verdict: dict) -> bool:
    """判断是否需要跑 security-agent。

    优先采信 review-agent 裁决中的 needs_security_review；
    若裁决未提供（为 False 且无明确信息），回退到对任务文本的
    关键词初筛。这只是触发判断，不是质量裁决。
    """
    if review_verdict.get("needs_security_review"):
        return True
    text = (user_task or "").lower()
    return any(kw.lower() in text for kw in SECURITY_TRIGGER_KEYWORDS)


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
    }


# ── 主调度函数 ────────────────────────────────────────────
def run_pipeline(user_task: str) -> dict:
    """
    运行完整的开发流水线。

    参数：
        user_task   用户的原始任务描述

    返回：
        {
            "success": True/False,
            "results": 每个 agent 的输出记录,
            "final_output": 最终结果（docs-agent 的输出）,
            "human_intervention_required": True/False,
            "human_intervention_reason": 原因（如需人工介入）
        }

    门禁规则：
    - run_agent 的 success 只表示 API 调用成功；review / security 的
      结构化 verdict 为 fail 时，不进入 docs-agent。
    - fix-agent 最多 MAX_FIX_ROUNDS 轮；修完必须重新过 review-agent。
      两轮仍 FAIL → 人工介入，不再继续修。
    """
    print(f"\n{'#'*50}")
    print(f"# 任务开始")
    print(f"# {user_task[:80]}{'...' if len(user_task) > 80 else ''}")
    print(f"{'#'*50}")

    results = {}
    previous_output = None  # 上一个 agent 的输出，作为下一个的输入

    def run_stage(agent_name: str, message: str, extra_context=None):
        """跑一个 agent；API 失败返回 (None, failure_dict)，成功返回 (result, None)。"""
        import base_agent
        model = base_agent.AGENT_MODEL_MAP.get(agent_name, "deepseek-chat")
        print_stage(f"阶段：{agent_name}", agent_name, model)
        result = run_agent(
            agent_name=agent_name,
            user_message=message,
            extra_context=extra_context,
        )
        print_result(result)
        return result

    # ── 阶段 1-3：编排 → TDD 测试 → 实现 ──────────────────
    for agent_name in ["main-orchestrator", "test-agent", "coding-agent"]:
        result = run_stage(agent_name, user_task, extra_context=previous_output)
        results[agent_name] = result
        if not result["success"]:
            print(f"\n❌ {agent_name} 执行失败，流水线中断")
            return _api_failure(results, agent_name, result)
        previous_output = result["response"]

    # ── 阶段 4-6：审查 → 安全（按需）→ 修复循环 ───────────
    fix_rounds_used = 0
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
        if _needs_security_review(user_task, last_verdict):
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
            break

        if fix_rounds_used >= MAX_FIX_ROUNDS:
            print(f"\n🚨 fix-agent 已用满 {MAX_FIX_ROUNDS} 轮仍不通过，需要人工介入")
            return _human_intervention(
                results,
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
        # 循环回到 review-agent 重新审查（不直接进入 docs-agent）。

    # ── 阶段 7：文档（仅在审查全部通过后）─────────────────
    docs_result = run_stage("docs-agent", user_task, extra_context=previous_output)
    results["docs-agent"] = docs_result
    if not docs_result["success"]:
        print("\n❌ docs-agent 执行失败，流水线中断")
        return _api_failure(results, "docs-agent", docs_result)

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
    }


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
    import sys

    if len(sys.argv) < 2:
        print("用法：")
        print("  完整流水线：python orchestrator.py '你的任务描述'")
        print("  单独调试：  python orchestrator.py debug <agent名称> '消息'")
        sys.exit(1)

    if sys.argv[1] == "debug" and len(sys.argv) >= 4:
        # 单独调试某个 agent
        run_single(agent_name=sys.argv[2], message=sys.argv[3])
    else:
        # 完整流水线
        task = sys.argv[1]
        result = run_pipeline(task)

        if result["human_intervention_required"]:
            print("\n" + "!" * 50)
            print("需要人工介入：")
            print(result["human_intervention_reason"])
            print("!" * 50)
