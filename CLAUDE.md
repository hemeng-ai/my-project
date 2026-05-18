# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

这是一个基于 Claude Code Agent 机制的多智能体编排开发流水线。通过 1 个中央编排器 + 6 个专业子 agent 协同工作，覆盖从需求设计到文档输出的完整开发生命周期。

## 入口规则

收到用户开发需求后，按以下优先级判断：

1. **新功能 / 架构变更 / 多文件改动** → `Agent(subagent_type="main-orchestrator")` 走完整四阶段流水线
2. **单文件 bug 修复 / 文案调整** → 简化流程：确认需求 → 派发 `fix-agent` → `review-agent` 审查
3. **纯问答 / 解释说明** → 直接回答，不启动流水线

## 架构

```
main-orchestrator (总指挥, 不写代码)
    │
    ├─ 阶段1: 头脑风暴 → Skill: superpowers:brainstorming
    ├─ 阶段2: 编写计划 → Skill: superpowers:writing-plans
    ├─ 阶段3: 执行循环 (每个 Task 按序派发)
    │   ├── test-agent (sonnet, 读/写) → 写失败测试
    │   ├── coding-agent (sonnet, 读/写) → 最简实现
    │   ├── security-agent (opus, 只读, 按需触发) → 安全扫描
    │   ├── review-agent (opus, 只读) → 两阶段审查
    │   └── fix-agent (sonnet, 读/写, 按需触发) → 最小修复 (上限2轮)
    └─ 阶段4: 收尾
        ├── docs-agent (sonnet, 读/写) → 生成文档
        └── Skill: superpowers:finishing-a-development-branch
```

## Agent 速查

| Agent | 模型 | 权限 | 触发条件 |
|---|---|---|---|
| `main-orchestrator` | 继承父会话 | 读写 + Skill + Agent | 新功能/架构变更/多文件改动 |
| `test-agent` | sonnet | 读写 | 每个实施任务之前（TDD 红灯） |
| `coding-agent` | sonnet | 读写 | test-agent 完成后 |
| `review-agent` | opus | **只读** | 每个任务完成后，必须触发 |
| `security-agent` | opus | **只读** | 涉及认证/输入/敏感数据/外部 API 时 |
| `fix-agent` | sonnet | 读写 | review 发现问题或测试失败时（上限 2 轮） |
| `docs-agent` | sonnet | 读写 | 所有审查通过后 |

## 关键设计约束

- **隔离原则**：子 agent 之间通过 git commit/文件传递状态，禁止依赖会话上下文
- **模型分层**：审查/安全用 opus（判断力），实现/测试/修复/文档用 sonnet（执行效率）
- **权限最小化**：review-agent 和 security-agent 只有只读工具（Read, Grep, Glob, Bash）
- **硬性门槛**：设计文档未获用户批准前，禁止派发任何实施任务
- **修复上限**：fix-agent 最多 2 轮，超出后上报阻塞

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

## 对 Agent 文件的修改约定

- Agent 文件使用 YAML frontmatter + Markdown 正文
- 修改 agent 行为时只改正文内容，不动 frontmatter 的 name/tools/model 字段
- 角色定义中的「你不做的事」禁止删除——这是防止越权的关键约束
- 工作流程用 ASCII 缩进图，保持与现有 agent 格式一致
