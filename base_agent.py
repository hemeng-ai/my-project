"""
base_agent.py — 负责"怎么运行一个 agent"

每次调用都是独立的 API 请求，上下文完全隔离。
只有 DeepSeek API，用不同模型区分强弱。
"""

import os
import json
from pathlib import Path
from openai import OpenAI  # DeepSeek 兼容 OpenAI SDK

# ── 模型配置 ──────────────────────────────────────────────
# deepseek-v4-pro   = 强模型，思考+判断类 agent，支持思考模式（reasoning_effort=max）
# deepseek-v4-flash = 快模型，执行类 agent，速度快且便宜

AGENT_MODEL_MAP = {
    # 前四个：判断/推理/审查 → deepseek-v4-pro
    "main-orchestrator": "deepseek-v4-pro",   # 任务拆解和调度
    "reflect-agent":     "deepseek-v4-pro",   # 需求一致性验证
    "review-agent":      "deepseek-v4-pro",   # 代码质量审查
    "security-agent":    "deepseek-v4-pro",   # 安全检查
    # 后四个：执行/生成类 → deepseek-v4-flash
    "coding-agent":      "deepseek-v4-flash", # 写代码
    "fix-agent":         "deepseek-v4-flash", # 修 bug
    "test-agent":        "deepseek-v4-flash", # 写测试
    "docs-agent":        "deepseek-v4-flash", # 写文档
}

# md 文件路径（相对于项目根目录）
AGENTS_DIR = Path(".claude/agents")

# ── DeepSeek 客户端 ───────────────────────────────────────
def get_client() -> OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "未找到 DEEPSEEK_API_KEY，请在 .env 文件中配置：\n"
            "DEEPSEEK_API_KEY=your_key_here"
        )
    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com"
    )

# ── 读取 agent 的 md 文件 ─────────────────────────────────
def load_agent_prompt(agent_name: str) -> str:
    md_path = AGENTS_DIR / f"{agent_name}.md"
    if not md_path.exists():
        raise FileNotFoundError(
            f"找不到 agent 文件：{md_path}\n"
            f"请确认文件存在于 .claude/agents/{agent_name}.md"
        )
    content = md_path.read_text(encoding="utf-8")

    # 去掉 frontmatter（--- 包裹的部分），只保留正文作为 system prompt
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()
    return content

# ── 核心运行函数 ──────────────────────────────────────────
def run_agent(
    agent_name: str,
    user_message: str,
    conversation_history: list = None,
    extra_context: str = None
) -> dict:
    """
    运行指定 agent，返回结果。

    参数：
        agent_name          agent 名称，必须在 AGENT_MODEL_MAP 中
        user_message        本次发给 agent 的消息
        conversation_history 多轮对话历史（可选），格式：
                            [{"role": "user", "content": "..."}, ...]
        extra_context       附加上下文（可选），如上游 agent 的输出

    返回：
        {
            "agent": agent 名称,
            "model": 使用的模型,
            "response": agent 的回复文本,
            "success": True/False,
            "error": 错误信息（仅在 success=False 时存在）
        }
    """
    if agent_name not in AGENT_MODEL_MAP:
        return {
            "agent": agent_name,
            "model": None,
            "response": None,
            "success": False,
            "error": f"未知的 agent：{agent_name}，可用列表：{list(AGENT_MODEL_MAP.keys())}"
        }

    model = AGENT_MODEL_MAP[agent_name]

    try:
        system_prompt = load_agent_prompt(agent_name)
        client = get_client()

        # 构造消息列表（全新上下文，只包含本次任务相关内容）
        messages = []

        # 如果有附加上下文（如上游 agent 的输出），拼到 user_message 前面
        if extra_context:
            full_message = f"【上游输入】\n{extra_context}\n\n【当前任务】\n{user_message}"
        else:
            full_message = user_message

        # 如果有多轮历史，加入（用于反思循环等需要记忆的场景）
        if conversation_history:
            messages.extend(conversation_history)

        messages.append({"role": "user", "content": full_message})

        # v4-pro 开启思考模式（reasoning_effort=max），v4-flash 不需要
        extra_params = {}
        if model == "deepseek-v4-pro":
            extra_params["extra_body"] = {"thinking": {"type": "enabled", "reasoning_effort": "max"}}

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            system=system_prompt,
            max_tokens=8192,       # 思考模式输出更长，调大一些
            temperature=0.2,       # 低温度，输出更稳定
            **extra_params
        )

        reply = response.choices[0].message.content

        return {
            "agent": agent_name,
            "model": model,
            "response": reply,
            "success": True
        }

    except FileNotFoundError as e:
        return {"agent": agent_name, "model": model, "response": None, "success": False, "error": str(e)}
    except EnvironmentError as e:
        return {"agent": agent_name, "model": model, "response": None, "success": False, "error": str(e)}
    except Exception as e:
        return {"agent": agent_name, "model": model, "response": None, "success": False, "error": f"API 调用失败：{str(e)}"}


# ── 快捷测试函数 ──────────────────────────────────────────
if __name__ == "__main__":
    print("=== base_agent 测试 ===\n")
    result = run_agent(
        agent_name="docs-agent",
        user_message="请简单介绍一下你的职责是什么。"
    )
    if result["success"]:
        print(f"Agent:  {result['agent']}")
        print(f"Model:  {result['model']}")
        print(f"回复:\n{result['response']}")
    else:
        print(f"失败：{result['error']}")
