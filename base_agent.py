"""
base_agent.py — 负责"怎么运行一个 agent"

每次调用都是独立的 API 请求，上下文完全隔离。
只有 DeepSeek API，用不同模型区分强弱。

注意：本文件只负责调用 DeepSeek API 并返回文本，不读写项目文件、
不运行测试。真正的文件编辑和测试运行发生在 Claude Code 里
（见 CLAUDE.md「运行时的区别」一节）。
"""

import os
import time
import json
from pathlib import Path
from openai import OpenAI  # DeepSeek 兼容 OpenAI SDK

# ── 模型配置 ──────────────────────────────────────────────
# deepseek-v4-pro   = 强模型，思考+判断类 agent，支持思考模式（reasoning_effort=max）
# deepseek-v4-flash = 快模型，执行类 agent，速度快且便宜

# 规范流水线（与 CLAUDE.md、orchestrator.py PIPELINE 保持一致，
# 1 个编排器 + 6 个专业子 agent，共 7 个）：
#   判断类 → Pro：main-orchestrator、review-agent、security-agent
#   执行类 → Flash：test-agent、coding-agent、fix-agent、docs-agent
# 已删除 reflect-agent：需求一致性验证并入 review-agent 的
# 两阶段审查（spec 合规 + 代码质量），不再单独设一个 agent。
AGENT_MODEL_MAP = {
    # 判断/推理/审查 → deepseek-v4-pro
    "main-orchestrator": "deepseek-v4-pro",   # 任务拆解和调度
    "review-agent":      "deepseek-v4-pro",   # 代码质量审查（两阶段）
    "security-agent":    "deepseek-v4-pro",   # 安全检查
    # 执行/生成类 → deepseek-v4-flash
    "coding-agent":      "deepseek-v4-flash", # 写代码
    "fix-agent":         "deepseek-v4-flash", # 修 bug
    "test-agent":        "deepseek-v4-flash", # 写测试
    "docs-agent":        "deepseek-v4-flash", # 写文档
}

# ── 用量 / 成本追踪（P1）─────────────────────────────────────
# 每次 API 调用返回的 usage（prompt/completion tokens）会累加到
# COST_TRACKER；成本按模型单价估算，单价默认值是估算值，请按当前
# 官方价目用环境变量覆盖（见 _model_price）。预算由环境变量控制：
#   PIPELINE_MAX_COST_USD（默认 5.0 美元）
#   PIPELINE_MAX_TOKENS（默认 1_000_000 tokens）
# 超预算时 run_agent 在调用前直接返回失败，不再烧钱；orchestrator
# 会把这种情况转为人工介入。
_MODEL_PRICE_USD_PER_MTOK = {
    # (input, output)，单位：美元 / 百万 tokens。默认值是估算。
    "deepseek-v4-pro": (2.0, 8.0),
    "deepseek-v4-flash": (0.1, 0.4),
    "deepseek-chat": (0.2, 0.8),
}
MAX_RETRY_ATTEMPTS = 3
API_TIMEOUT_SECONDS = 120


def _model_price(model):
    import os as _os
    key = model.replace("-", "_").upper()
    inp = float(_os.getenv(f"PIPELINE_PRICE_{key}_INPUT", _MODEL_PRICE_USD_PER_MTOK.get(model, (0.0, 0.0))[0]))
    out = float(_os.getenv(f"PIPELINE_PRICE_{key}_OUTPUT", _MODEL_PRICE_USD_PER_MTOK.get(model, (0.0, 0.0))[1]))
    return inp, out


def _budget_limits():
    max_cost = float(os.getenv("PIPELINE_MAX_COST_USD", "5.0"))
    max_tokens = int(os.getenv("PIPELINE_MAX_TOKENS", "1000000"))
    return max_cost, max_tokens


class CostTracker:
    """累计用量与成本。只记数字，不记任何密钥或正文。"""

    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_cost_usd = 0.0
        self.calls = 0

    def add(self, model, usage):
        if not usage:
            return 0.0
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        inp_price, out_price = _model_price(model)
        cost = prompt / 1_000_000 * inp_price + completion / 1_000_000 * out_price
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.estimated_cost_usd += cost
        self.calls += 1
        return cost

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens

    def exceeded(self):
        max_cost, max_tokens = _budget_limits()
        return self.estimated_cost_usd > max_cost or self.total_tokens > max_tokens

    def summary(self):
        max_cost, max_tokens = _budget_limits()
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "max_cost_usd": max_cost,
            "max_tokens": max_tokens,
            "exceeded": self.exceeded(),
        }


COST_TRACKER = CostTracker()


def get_cost_summary():
    return COST_TRACKER.summary()


def reset_cost_tracker():
    global COST_TRACKER
    COST_TRACKER = CostTracker()


_RETRYABLE_MARKERS = ("429", "rate_limit", "rate limit", "timeout", "timed out", "502", "503", "500", "overloaded")


def _is_retryable(exc):
    text = str(exc).lower()
    return any(m in text for m in _RETRYABLE_MARKERS)


# md 文件路径（相对于项目根目录）
# 真实文件是嵌套的：.claude/agents/{name}/{name}.md
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
    # 先试嵌套路径（仓库真实布局），再回退到扁平路径（兼容旧布局）。
    nested_path = AGENTS_DIR / agent_name / f"{agent_name}.md"
    flat_path = AGENTS_DIR / f"{agent_name}.md"
    tried = [nested_path, flat_path]
    md_path = next((p for p in tried if p.exists()), None)
    if md_path is None:
        raise FileNotFoundError(
            f"找不到 agent 文件：{agent_name}\n"
            f"已尝试的路径：\n"
            + "\n".join(f"  - {p}" for p in tried)
            + "\n请确认文件存在于 .claude/agents/{name}/{name}.md"
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
            "success": True/False,   # 仅表示 API 调用成功，不表示质量审查通过。
                                     # 重试最多 3 次（仅限流/超时/5xx），用量记入 COST_TRACKER。
                                     # 质量裁决见 orchestrator.py 的 parse_verdict，
                                     # review FAIL 会阻塞流水线。
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

        # 如果有多轮历史，加入（用于修复循环等需要记忆的场景）
        if conversation_history:
            messages.extend(conversation_history)

        messages.append({"role": "user", "content": full_message})

        # v4-pro 开启思考模式（reasoning_effort=max），v4-flash 不需要
        extra_params = {}
        if model == "deepseek-v4-pro":
            extra_params["extra_body"] = {"thinking": {"type": "enabled", "reasoning_effort": "max"}}

        # 预算先行：超预算就不发请求，直接失败，由 orchestrator 转人工介入。
        if COST_TRACKER.exceeded():
            summary = COST_TRACKER.summary()
            return {
                "agent": agent_name,
                "model": model,
                "response": None,
                "success": False,
                "error": (
                    "成本/用量预算已超限，不再调用 API："
                    f"已用 {summary['total_tokens']} tokens / "
                    f"约 ${summary['estimated_cost_usd']}（上限 "
                    f"{summary['max_tokens']} tokens / ${summary['max_cost_usd']}）。"
                    "请调高 PIPELINE_MAX_TOKENS / PIPELINE_MAX_COST_USD 后重试，或人工介入。"
                ),
                "usage": None,
                "estimated_cost_usd": 0.0,
            }

        # 重试：只对限流 / 超时 / 5xx 类临时错误重试（指数退避，最多 3 次）。
        # 4xx 认证/参数错误不重试，直接失败。
        response = None
        last_exc = None
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    system=system_prompt,
                    max_tokens=8192,       # 思考模式输出更长，调大一些
                    temperature=0.2,       # 低温度，输出更稳定
                    timeout=API_TIMEOUT_SECONDS,
                    **extra_params
                )
                break
            except Exception as exc:  # noqa: BLE001 - 需区分可重试与不可重试错误
                last_exc = exc
                if not _is_retryable(exc) or attempt == MAX_RETRY_ATTEMPTS:
                    raise
                time.sleep(2 ** (attempt - 1))
        if response is None:
            raise last_exc

        reply = response.choices[0].message.content
        usage = getattr(response, "usage", None)
        call_cost = COST_TRACKER.add(model, usage)

        return {
            "agent": agent_name,
            "model": model,
            "response": reply,
            "success": True,
            "usage": {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0) if usage else 0,
            } if usage else None,
            "estimated_cost_usd": round(call_cost, 4),
            "cost_summary": COST_TRACKER.summary(),
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
