#!/usr/bin/env bash
# Git Worktree 自动创建脚本（bash 版，macOS / Linux / 云端用）
# 与 git-worktree-setup.ps1 做同一件事：在 SessionStart 时运行，
# 确保开发在隔离的 worktree 中进行。
# 用法：bash .claude/hooks/git-worktree-setup.sh
# （settings.json 里默认配的是 PowerShell 版；在非 Windows 上把
#  那条命令换成上面这条，并先 chmod +x 本文件。）
set -u
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_ROOT" || exit 0

# 检查是否有至少一个 commit（worktree 需要基于分支创建）
if ! git rev-list --count HEAD >/dev/null 2>&1; then
  echo "[WORKTREE] 仓库尚无提交，跳过 worktree 创建，直接在默认分支上工作"
  exit 0
fi
if [ "$(git rev-list --count HEAD 2>/dev/null)" = "0" ]; then
  echo "[WORKTREE] 仓库尚无提交，跳过 worktree 创建，直接在默认分支上工作"
  exit 0
fi

# 检查是否已在 worktree 中
GIT_DIR="$(git rev-parse --git-dir 2>/dev/null || true)"
case "$GIT_DIR" in
  *worktrees*)
    echo "[WORKTREE] 已在 worktree 中，跳过创建"
    exit 0
    ;;
esac

# 获取当前分支名
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "[WORKTREE] 无法检测当前分支，跳过创建"
  exit 0
fi

# 不在 main/master 上时才创建 worktree
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  echo "[WORKTREE] 当前在 $BRANCH 分支，跳过创建（直接在主干上工作）"
  exit 0
fi

# 检查是否已有对应的 worktree
WORKTREE_DIR=".claude/worktrees/$BRANCH"
if [ -d "$WORKTREE_DIR" ]; then
  echo "[WORKTREE] worktree 已存在: $WORKTREE_DIR"
  exit 0
fi

# 创建 worktree（注意：git 不允许同一个分支同时检出在两个 worktree 里。
# 当前分支已检出在主工作区时，直接 git worktree add <目录> <分支> 会失败，
# 这是原 PowerShell 版也有的坑：它不检查退出码，会谎称“已就绪”。
# 这里先正常尝试，失败则回退为 detached worktree（指向当前 HEAD），
# 并在输出里如实说明；两种情况都失败时只报警告，不让 hook 失败整个会话。）
echo "[WORKTREE] 正在创建隔离工作区: $WORKTREE_DIR ..."
mkdir -p ".claude/worktrees"
if git worktree add "$WORKTREE_DIR" "$BRANCH" 2>/dev/null; then
  echo "[WORKTREE] 隔离工作区已就绪: $PROJECT_ROOT/$WORKTREE_DIR"
  echo "[WORKTREE] 请在 $WORKTREE_DIR 中进行开发"
elif git worktree add --detach "$WORKTREE_DIR" HEAD 2>/dev/null; then
  echo "[WORKTREE] 分支 $BRANCH 已检出在主工作区，已创建 detached worktree（指向当前 HEAD）: $PROJECT_ROOT/$WORKTREE_DIR"
  echo "[WORKTREE] 在其中提交前请先新建分支（git switch -c <新分支>）"
else
  echo "[WORKTREE] 警告：创建 worktree 失败，请手动检查（git worktree list）"
fi
