# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

这是一个基于 Claude Code Agent 机制的多智能体编排开发流水线。通过 1 个中央编排器 + 6 个专业子 agent 协同工作，覆盖从需求设计到文档输出的完整开发生命周期。

## 入口规则

收到用户开发需求后，按以下优先级判断：

1. **新功能 / 架构变更 / 多文件改动** → 立即激活 `main-orchestrator`，走完整四阶段流水线
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

## 关键设计约束

- **隔离原则**：子 agent 之间通过 git commit/文件传递状态，禁止依赖会话上下文
- **模型分层**：审查/安全用 opus（判断力），实现/测试/修复/文档用 sonnet（执行效率）
- **权限最小化**：review-agent 和 security-agent 只有只读工具（Read, Grep, Glob, Bash）
- **硬性门槛**：设计文档未获用户批准前，禁止派发任何实施任务
- **修复上限**：fix-agent 最多 2 轮，超出后上报阻塞

## 项目结构

```
.claude/agents/
├── main-orchestrator/main-orchestrator.md
├── coding-agent/coding-agent.md
├── test-agent/test-agent.md
├── review-agent/review-agent.md
├── security-agent/security-agent.md
├── fix-agent/fix-agent.md
└── docs-agent/docs-agent.md
```

## 依赖的 Skills

本流水线依赖以下 Superpowers 技能（已通过插件安装）：

- `superpowers:brainstorming` — 需求设计
- `superpowers:writing-plans` — 编写实施计划
- `superpowers:subagent-driven-development` — 子 agent 驱动执行
- `superpowers:executing-plans` — 串行批量执行
- `superpowers:dispatching-parallel-agents` — 并行任务分发
- `superpowers:using-git-worktrees` — 隔离工作区
- `superpowers:finishing-a-development-branch` — 分支收尾
- `superpowers:verification-before-completion` — 完成前验证

## 对 Agent 文件的修改约定

- Agent 文件使用 YAML frontmatter + Markdown 正文
- 修改 agent 行为时只改正文内容，不动 frontmatter 的 name/tools/model 字段
- 角色定义中的「你不做的事」禁止删除——这是防止越权的关键约束
- 工作流程用 ASCII 缩进图，保持与现有 agent 格式一致
