"""
orchestrator.py — 负责"找哪个 agent"

接收用户任务 → 判断路由 → 按顺序调用 agent 流水线 → 返回最终结果。
包含反思循环（最多 2 轮）和人工介入触发。
"""

import json
from base_agent import run_agent

# ── 流水线定义 ────────────────────────────────────────────
# 标准全栈开发任务的 agent 执行顺序
PIPELINE = [
    "main-orchestrator",   # 1. 拆解任务，生成执行计划
    "test-agent",          # 2. TDD：先写失败测试
    "coding-agent",        # 3. 实现代码
    "reflect-agent",       # 4. 验证代码是否符合原始需求（最多 2 轮）
    "review-agent",        # 5. 代码质量审查
    "security-agent",      # 6. 安全检查
    "docs-agent",          # 7. 生成文档
]

# 反思循环最大次数
MAX_REFLECT_ROUNDS = 2

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

def is_reflect_passed(reflect_response: str) -> bool:
    """判断 reflect-agent 是否通过（简单关键词判断）"""
    pass_keywords = ["验证通过", "需求一致", "可进入下一阶段", "放行"]
    fail_keywords = ["未实现", "不通过", "人工介入", "❌", "⚠️"]

    response_lower = reflect_response.lower()
    for kw in pass_keywords:
        if kw in reflect_response:
            return True
    for kw in fail_keywords:
        if kw in reflect_response:
            return False
    # 默认通过（避免误拦截）
    return True

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
    """
    print(f"\n{'#'*50}")
    print(f"# 任务开始")
    print(f"# {user_task[:80]}{'...' if len(user_task) > 80 else ''}")
    print(f"{'#'*50}")

    results = {}
    previous_output = None  # 上一个 agent 的输出，作为下一个的输入

    for agent_name in PIPELINE:

        # ── reflect-agent 特殊处理：反思循环 ──────────────
        if agent_name == "reflect-agent":
            coding_output = results.get("coding-agent", {}).get("response", "")
            reflect_result = None

            for round_num in range(1, MAX_REFLECT_ROUNDS + 1):
                print_stage(f"需求一致性验证（第 {round_num} 轮）", agent_name, "deepseek-reasoner")

                reflect_message = (
                    f"原始任务需求：\n{user_task}\n\n"
                    f"coding-agent 的输出：\n{coding_output}\n\n"
                    f"当前是第 {round_num} 轮反思，请验证代码是否完整实现了所有需求。"
                )

                if round_num == 2 and reflect_result:
                    reflect_message += f"\n\n第 1 轮反思报告：\n{reflect_result['response']}"

                reflect_result = run_agent(
                    agent_name="reflect-agent",
                    user_message=reflect_message,
                )
                print_result(reflect_result)

                if not reflect_result["success"]:
                    break

                # 判断是否通过
                if is_reflect_passed(reflect_result["response"]):
                    print(f"\n✅ 第 {round_num} 轮反思通过，继续流水线")
                    results[agent_name] = reflect_result
                    previous_output = reflect_result["response"]
                    break

                # 第 1 轮不通过，返回 coding-agent 重写
                if round_num == 1:
                    print(f"\n⚠️  第 1 轮反思不通过，返回 coding-agent 重写...")
                    recode_result = run_agent(
                        agent_name="coding-agent",
                        user_message=user_task,
                        extra_context=f"reflect-agent 的反馈（请针对性修改）：\n{reflect_result['response']}"
                    )
                    print_result(recode_result)
                    if recode_result["success"]:
                        coding_output = recode_result["response"]
                        results["coding-agent-round2"] = recode_result

                # 第 2 轮不通过，触发人工介入
                elif round_num == MAX_REFLECT_ROUNDS:
                    print(f"\n🚨 第 2 轮反思仍不通过，需要人工介入")
                    results[agent_name] = reflect_result
                    return {
                        "success": False,
                        "results": results,
                        "final_output": None,
                        "human_intervention_required": True,
                        "human_intervention_reason": (
                            f"reflect-agent 经过 {MAX_REFLECT_ROUNDS} 轮反思后仍存在需求偏差。\n"
                            f"请判断：\n"
                            f"  A. 代码方向跑偏 → 否决，重新描述需求\n"
                            f"  B. reflect-agent 判断有误 → 放行，修正 reflect-agent.md\n"
                            f"  C. 需求描述不清晰 → 重新澄清需求后重启\n\n"
                            f"最后一次反思报告：\n{reflect_result['response']}"
                        )
                    }
            continue  # reflect-agent 已处理，跳过下面的通用逻辑

        # ── 其他 agent 通用处理 ───────────────────────────
        model = __import__('base_agent').AGENT_MODEL_MAP.get(agent_name, "deepseek-chat")
        print_stage(
            stage=f"阶段：{agent_name}",
            agent=agent_name,
            model=model
        )

        result = run_agent(
            agent_name=agent_name,
            user_message=user_task,
            extra_context=previous_output
        )
        print_result(result)

        results[agent_name] = result

        if not result["success"]:
            print(f"\n❌ {agent_name} 执行失败，流水线中断")
            return {
                "success": False,
                "results": results,
                "final_output": None,
                "human_intervention_required": False,
                "human_intervention_reason": f"{agent_name} 执行失败：{result.get('error')}"
            }

        previous_output = result["response"]

    # ── 全部通过 ──────────────────────────────────────────
    print(f"\n{'#'*50}")
    print("# ✅ 流水线全部完成")
    print(f"{'#'*50}\n")

    return {
        "success": True,
        "results": results,
        "final_output": results.get("docs-agent", {}).get("response"),
        "human_intervention_required": False,
        "human_intervention_reason": None
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
            print("\n" + "!"*50)
            print("需要人工介入：")
            print(result["human_intervention_reason"])
            print("!"*50)
