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

- **Python 编排器（`orchestrator.py` / `base_agent.py`）：** 只调用 DeepSeek API 并返回文本，不编辑项目文件。它的 `run_agent` 只是发一次独立的 API 请求，上下文完全隔离。测试运行现在由 `gates.py` 真正执行（P1：跑 pytest/jest/go test 并按结果做门禁），但代码修改仍只发生在 Claude Code 里。
- **Claude Code 运行时：** agent 的 `.md` 文件里声明的 `tools:`（Read、Write、Edit、Bash 等）才真正生效，文件编辑、测试运行、git 操作都在这里发生。`review-agent` 和 `security-agent` 的只读约束也是靠这里的工具列表 enforcement 的，Python 编排器里没有工具可供执行。

## 关键设计约束

- **隔离原则**：子 agent 之间通过 git commit/文件传递状态，禁止依赖会话上下文
- **模型分层**：审查/安全用 Pro（判断力），实现/测试/修复/文档用 Flash（执行效率）
- **权限最小化**：review-agent 和 security-agent 只有只读工具（Read, Grep, Glob, Bash）
- **硬性门槛**：设计文档未获用户批准前，禁止派发任何实施任务
- **修复上限**：fix-agent 最多 2 轮，修完必须重新审查，超出后上报阻塞

## P1：真实门禁、持久化、重试与成本、任务队列

P0 只修了“裁决是不是真的拦得住”，P1 补的是“门禁是不是真的跑过”：

- **真实门禁（`gates.py`）**：test-agent 之后跑测试，红灯必须失败且失败原因只能是“未实现”（断言 / NotImplementedError）；语法错误（SyntaxError）或环境错误（ModuleNotFoundError / ImportError）直接触发人工介入，不派 fix-agent 改测试。coding-agent 之后跑测试，必须全过，否则进 fix 循环；fix 之后重跑测试并做回归检查（新出现的失败会作为下一轮修复的输入）。lint 是 best-effort：没有配置 linter 时跳过，配置了但失败时记入日志并追加到审查上下文，不单独硬拦。git diff / git status 用于记录变更文件，也作为安全触发的输入之一。
- **持久化与断点续跑**：每次运行写检查点 `.pipeline-state.json`（已完成的阶段、修复轮次、基线、上一阶段输出）和 JSONL 运行日志 `pipeline-run.log.jsonl`（每行一个阶段：时间、agent、模型、裁决、耗时、成本汇总）。`python orchestrator.py --resume '任务'` 会从检查点恢复，跳过已完成的阶段。这两个文件是本地运行状态，不要提交（已加入 `.gitignore`）。
- **重试与成本（`base_agent.py`）**：DeepSeek API 调用失败时只对限流 / 超时 / 5xx 重试（指数退避，最多 3 次，单次超时 120 秒），401 等认证错误不重试。每次调用的 usage 会累加到 `COST_TRACKER`，成本按模型单价估算（默认值是估算，请按当前官方价目用环境变量覆盖）。预算由 `PIPELINE_MAX_COST_USD`（默认 5.0 美元）和 `PIPELINE_MAX_TOKENS`（默认 1,000,000）控制，超预算时不再发请求，直接转人工介入。
- **任务队列与 WIP 上限**：`python orchestrator.py queue tasks.jsonl` 按序跑多个独立任务（JSONL，每行 `{"title": ..., "task": ...}`），一个任务失败不中断整队，结果写进 `tasks.jsonl.results.jsonl`。WIP 上限：coding-agent、review-agent、security-agent、main-orchestrator、docs-agent 同时最多 1 个，test-agent、fix-agent 最多 2 个。单任务内的 test → coding → review 必须串行；队列是按序跑的，不做多线程并行执行（多线程会打乱 git 状态和检查点），并行只体现在任务之间互不阻塞。

## P2：结构化输出、安全触发、基线回滚

- **结构化输出**：review-agent、security-agent、fix-agent、test-agent 的 `.md` 输出规范里都要求在末尾附一段机器可读的 JSON 块（格式见各自文件），与 `orchestrator.py` 的 `parse_verdict` 对应。人类可读的部分保留，JSON 块只做机器判断。
- **安全触发（单一事实来源）**：触发类别定义在 `orchestrator.py` 的 `SECURITY_TRIGGERS` 里：auth（认证/登录/密码/token/权限）、input（用户输入/输入校验/文件上传）、sensitive_data（敏感数据/隐私/支付）、external_api（外部 API/第三方/webhook）。security-agent 只在触发类别命中任务文本或变更文件、或 review-agent 的裁决里 `needs_security_review` 为 true 时才跑；`CLAUDE.md` 和 `security-agent.md` 里的触发表与这份定义保持一致。
- **基线与回滚**：coding-agent 运行前，流水线记录基线——当前 git HEAD SHA + 变更摘要——写进检查点。fix-agent 用满 2 轮仍失败、或测试回归时，失败报告里会附上基线 SHA。回滚方法是人工执行：`git stash` 或 `git checkout <基线 SHA> -- <相关文件>`，确认测试恢复后再重新派发任务。**不要无确认执行 `git reset --hard`**。

## 项目专属规则（不属于通用流水线）

以下规则只适用于 sports-injury-platform 项目，不要当成通用流水线规则用到别的项目上：

- **JWT 解密原则（仅 sports-injury-platform）**：NextAuth v5 使用 JWE (A256CBC-HS512) 加密 session token，加密密钥通过 HKDF-SHA256 从 AUTH_SECRET 派生。**禁止自定义 HKDF 派生逻辑或直接传原始 secret 给 jwtDecrypt**，必须统一使用 `@auth/core/jwt` 的 `decode()` 函数。详见 `src/lib/session.ts` 顶部注释。

## 项目结构

```
.claude/
├── settings.json          # hooks 配置（SessionStart + SessionEnd）
├── hooks/
│   ├── git-worktree-setup.ps1   # Windows: SessionStart 自动创建隔离 worktree
│   ├── git-worktree-setup.sh    # macOS/Linux/云端：同上（bash 版，见下）
│   ├── session-end.ps1          # Windows: SessionEnd 自动提交 + 验证 + 清理
│   └── session-end.sh           # macOS/Linux/云端：同上（bash 版，见下）
└── agents/
    ├── main-orchestrator/main-orchestrator.md
    ├── coding-agent/coding-agent.md
    ├── test-agent/test-agent.md
    ├── review-agent/review-agent.md
    ├── security-agent/security-agent.md
    ├── fix-agent/fix-agent.md
    └── docs-agent/docs-agent.md
```

新增文件：`gates.py`（真实门禁：跑测试 / lint / git diff）、`pipeline-run.log.jsonl` 与 `.pipeline-state.json`（本地运行状态，不提交）。

## Hooks 自动化

| Hook | 触发时机 | 脚本 | 功能 |
|---|---|---|---|
| SessionStart | 会话启动 | `git-worktree-setup.ps1`（Windows）或 `git-worktree-setup.sh`（macOS/Linux/云端） | 自动创建隔离 worktree |
| SessionEnd | 会话结束 | `session-end.ps1`（Windows）或 `session-end.sh`（macOS/Linux/云端） | 自动提交 → lint/test 验证 → 清理已合并 worktree |

PowerShell 版是 Windows 专用的，bash 版是 macOS / Linux / 云端用的，两套做的是同一件事（创建隔离 worktree；结束时提交、验证、清理已合并的 worktree）。`settings.json` 里默认配的是 PowerShell 版；在 macOS / Linux / 云端使用时，把其中两条命令换成对应的 `bash .claude/hooks/*.sh` 即可（先 `chmod +x .claude/hooks/*.sh`）。注意：bash 版 session-end 不重启 dev server（那是 sports-injury-platform 在 Windows 上的专属行为），需要的话手动启动。

`settings.json` 里不要写死本机路径。旧版里写死的 `d:\ClaudeWorkSpace\my_project\sports-injury-platform` 已删除；`additionalDirectories` 留空，需要额外目录时由每台机器自己在本地配置里加，不要提交进仓库。

## 环境前提

- **PowerShell ExecutionPolicy（仅 Windows）**：hooks 脚本需要 `RemoteSigned` 或 `Bypass` 级别
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```
- **bash hooks（macOS / Linux / 云端）**：使用前先 `chmod +x .claude/hooks/*.sh`；`settings.json` 里把 PowerShell 命令换成 `bash .claude/hooks/git-worktree-setup.sh` / `bash .claude/hooks/session-end.sh`。
- **Git 版本**：`git worktree` 需要 Git 2.5+，`git worktree remove` 需要 Git 2.17+
- **Python 编排器**：需要 Python 3.10+ 和 `openai` 包（DeepSeek 兼容 OpenAI SDK），以及 `DEEPSEEK_API_KEY` 环境变量；预算可用 `PIPELINE_MAX_COST_USD` / `PIPELINE_MAX_TOKENS` 控制。

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
- git 不允许同一个分支同时检出在两个 worktree 里：当前分支已检出在主工作区时，`git worktree add <目录> <分支>` 会失败。bash 版 hook 遇到这种情况会回退为 detached worktree（指向当前 HEAD）并如实说明；原 PowerShell 版不检查退出码，失败时也会谎称“已就绪”，需要手动用 `git worktree list` 核对。

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
