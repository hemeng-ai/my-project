# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

这是一个基于 Claude Code Agent 机制的多智能体编排开发流水线。通过 1 个中央编排器 + 6 个专业子 agent 协同工作，覆盖从需求设计到文档输出的完整开发生命周期。

## 规范流水线（单一事实来源）

以下顺序是唯一的规范流水线。`orchestrator.py` 的 `PIPELINE`、`base_agent.py` 的 `AGENT_MODEL_MAP` 和本文件必须保持一致，改一处就要同步改另外两处：

```
main-orchestrator (总指挥, 不写代码)
    │
    ├─ 阶段1: 头脑风暴 → Skill: superpowers:brainstorming
    ├─ 阶段2: 编写计划 → Skill: superpowers:writing-plans
    ├─ 阶段3: 执行循环 (每个 Task 按序派发)
    │   ├── test-agent → 写失败测试（TDD 红灯）
    │   ├── coding-agent → 最简实现
    │   ├── review-agent → 两阶段审查（spec 合规 → 代码质量，只读，必须触发）
    │   ├── security-agent → 安全扫描（只读，仅当涉及认证/输入/敏感数据/外部 API 时）
    │   └── fix-agent → 最小修复（上限 2 轮，修完必须重新过 review-agent）
    └─ 阶段4: 收尾
        ├── docs-agent → 生成文档（仅在审查全部通过后）
        └── Skill: superpowers:finishing-a-development-branch
```

已删除 `reflect-agent`：需求一致性验证并入 `review-agent` 的 spec 合规审查，不再单独设一个 agent；原来 `orchestrator.py` 里关键词判断放行的 `is_reflect_passed` 已删除，改为结构化 verdict（见下）。

## 入口规则

收到用户开发需求后，按以下优先级判断：

1. **新功能 / 架构变更 / 多文件改动** → `Agent(subagent_type="main-orchestrator")` 走完整四阶段流水线
2. **单文件 bug 修复 / 文案调整** → 简化流程：确认需求 → 派发 `fix-agent` → `review-agent` 审查（审查不通过同样走修复循环，上限 2 轮）
3. **纯问答 / 解释说明** → 直接回答，不启动流水线

## 架构

```
main-orchestrator (总指挥, 不写代码)
    │
    ├─ 阶段1: 头脑风暴 → Skill: superpowers:brainstorming
    ├─ 阶段2: 编写计划 → Skill: superpowers:writing-plans
    ├─ 阶段3: 执行循环 (每个 Task 按序派发)
    │   ├── test-agent (Flash, 读/写) → 写失败测试
    │   ├── coding-agent (Flash, 读/写) → 最简实现
    │   ├── review-agent (Pro, 只读) → 两阶段审查
    │   ├── security-agent (Pro, 只读, 按需触发) → 安全扫描
    │   └── fix-agent (Flash, 读/写, 按需触发) → 最小修复 (上限2轮，修完重审)
    └─ 阶段4: 收尾
        ├── docs-agent (Flash, 读/写) → 生成文档
        └── Skill: superpowers:finishing-a-development-branch
```

## Agent 速查

| Agent | 模型（Python 编排器） | Claude Code 对应 | 权限 | 触发条件 |
|---|---|---|---|---|
| `main-orchestrator` | deepseek-v4-pro | 继承父会话 | 读写 + Skill + Agent | 新功能/架构变更/多文件改动 |
| `test-agent` | deepseek-v4-flash | sonnet | 读写 | 每个实施任务之前（TDD 红灯） |
| `coding-agent` | deepseek-v4-flash | sonnet | 读写 | test-agent 完成后 |
| `review-agent` | deepseek-v4-pro | opus | **只读** | 每个任务完成后，必须触发 |
| `security-agent` | deepseek-v4-pro | opus | **只读** | 涉及认证/输入/敏感数据/外部 API 时 |
| `fix-agent` | deepseek-v4-flash | sonnet | 读写 | review 发现问题或测试失败时（上限 2 轮，修完重审） |
| `docs-agent` | deepseek-v4-flash | sonnet | 读写 | 所有审查通过后 |

模型分层：判断类（编排、审查、安全）用 Pro，执行类（测试、实现、修复、文档）用 Flash。Python 编排器里写的是 DeepSeek 模型名；Claude Code 里 agent `.md` 的 frontmatter 仍用 opus/sonnet，两者是同一分层的两种写法，不要混用。

## 审查裁决：结构化 verdict（门禁）

`review-agent` 和 `security-agent` 必须在回复末尾附一段 JSON 裁决，不能只写自然语言结论：

```json
{"verdict": "pass" | "fail", "critical": 0, "warning": 0, "needs_security_review": false, "issues": ["问题描述", "..."]}
```

裁决规则（`orchestrator.py` 的 `parse_verdict` 执行，`run_agent` 的 `success` 只是 API 调用成功，不代表通过）：

- 找不到可解析的 JSON verdict，或 verdict 不在 `pass` / `fail` 内 → 按 **FAIL** 处理，需人工复核，永远不默认通过。
- `critical > 0` → 强制 **FAIL**，即使模型写了 pass 也不放行。
- verdict 为 `fail` 时不进入 `docs-agent`：有修复轮次剩余时派 `fix-agent`（最多 2 轮），修完重新过 `review-agent`；两轮仍不通过 → 触发人工介入，流水线停止。
- `security-agent` 的裁决同样适用；未触发安全审查条件时记为跳过，不记为通过。

## 运行时的区别：Python 编排器 vs Claude Code

这是两套不同的运行时，不要混淆：

- **Python 编排器（`orchestrator.py` / `base_agent.py`）：** 只调用 DeepSeek API 并返回文本，不编辑项目文件、不运行测试。它的 `run_agent` 只是发一次独立的 API 请求，上下文完全隔离。不要声称它跑过测试，除非真的加了测试执行（本次 P0 范围外）。
- **Claude Code 运行时：** agent 的 `.md` 文件里声明的 `tools:`（Read、Write、Edit、Bash 等）才真正生效，文件编辑、测试运行、git 操作都在这里发生。`review-agent` 和 `security-agent` 的只读约束也是靠这里的工具列表 enforcement 的，Python 编排器里没有工具可供执行。

## 关键设计约束

- **隔离原则**：子 agent 之间通过 git commit/文件传递状态，禁止依赖会话上下文
- **模型分层**：审查/安全用 Pro（判断力），实现/测试/修复/文档用 Flash（执行效率）
- **权限最小化**：review-agent 和 security-agent 只有只读工具（Read, Grep, Glob, Bash）
- **硬性门槛**：设计文档未获用户批准前，禁止派发任何实施任务
- **修复上限**：fix-agent 最多 2 轮，修完必须重新审查，超出后上报阻塞
- **JWT 解密原则**：NextAuth v5 使用 JWE (A256CBC-HS512) 加密 session token，加密密钥通过 HKDF-SHA256 从 AUTH_SECRET 派生。**禁止自定义 HKDF 派生逻辑或直接传原始 secret 给 jwtDecrypt**，必须统一使用 `@auth/core/jwt` 的 `decode()` 函数。详见 `src/lib/session.ts` 顶部注释。

## 项目结构

```
.claude/
├── settings.json          # hooks 配置（SessionStart + SessionEnd）
├── hooks/
│   ├── git-worktree-setup.ps1   # SessionStart: 自动创建隔离 worktree
│   └── session-end.ps1          # SessionEnd: 自动提交 + 验证 + 清理
└── agents/
    ├── main-orchestrator/main-orchestrator.md
    ├── coding-agent/coding-agent.md
    ├── test-agent/test-agent.md
    ├── review-agent/review-agent.md
    ├── security-agent/security-agent.md
    ├── fix-agent/fix-agent.md
    └── docs-agent/docs-agent.md
```

## Hooks 自动化

| Hook | 触发时机 | 脚本 | 功能 |
|---|---|---|---|
| SessionStart | 会话启动 | `git-worktree-setup.ps1` | 自动创建隔离 worktree |
| SessionEnd | 会话结束 | `session-end.ps1` | 自动提交 → lint/test 验证 → 清理已合并 worktree |

## 环境前提

- **PowerShell ExecutionPolicy**：hooks 脚本需要 `RemoteSigned` 或 `Bypass` 级别
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```
- **Git 版本**：`git worktree` 需要 Git 2.5+，`git worktree remove` 需要 Git 2.17+

## 故障排查

### Hooks 未触发
1. 检查 PowerShell 执行策略：`Get-ExecutionPolicy`
2. 确认 `.claude/settings.json` 是合法 JSON（语法错误会导致 hooks 被静默忽略）
3. 手动运行脚本验证：
   ```powershell
   powershell -ExecutionPolicy Bypass -File ".claude/hooks/git-worktree-setup.ps1"
   powershell -ExecutionPolicy Bypass -File ".claude/hooks/session-end.ps1"
   ```

### Worktree 未创建
- worktree 需要至少一个 commit；空仓库会自动跳过
- 已在 worktree 中时不会重复创建
- 在 main / master 分支上工作时不会创建隔离区

### Agent 调用失败
- 确保 agent 的 `.md` 文件中 `tools:` frontmatter 包含所需工具
- review-agent 和 security-agent 只有只读工具，向其派发写操作会失败
- Python 编排器报“找不到 agent 文件”时，检查 `.claude/agents/{name}/{name}.md` 嵌套路径是否存在（`base_agent.py` 先试嵌套路径，再回退扁平路径）

### 审查不通过但流水线继续
- 这是旧版 bug，本版已修：如果还出现，先确认跑的是本版 `orchestrator.py`（带 `parse_verdict`），再检查 review-agent 回复末尾是否附了 JSON 裁决；没有 JSON 裁决时按 FAIL 处理，不会进入 docs-agent。

## 对 Agent 文件的修改约定

- Agent 文件使用 YAML frontmatter + Markdown 正文
- 修改 agent 行为时只改正文内容，不动 frontmatter 的 name/tools/model 字段
- 角色定义中的「你不做的事」禁止删除——这是防止越权的关键约束
- 工作流程用 ASCII 缩进图，保持与现有 agent 格式一致
