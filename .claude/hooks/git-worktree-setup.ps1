# Git Worktree 自动创建脚本
# 在 SessionStart 时运行，确保开发在隔离的 worktree 中进行

$ErrorActionPreference = "Stop"
$projectRoot = $env:CLAUDE_PROJECT_DIR
if (-not $projectRoot) { $projectRoot = (Get-Location).Path }

Set-Location $projectRoot

# 检查是否有至少一个 commit（worktree 需要基于分支创建）
$commitCount = git rev-list --count HEAD 2>$null
if (-not $commitCount -or $commitCount -eq "0") {
    # 尚无 commit，跳过 worktree 创建，直接在 main 上工作
    Write-Output "[WORKTREE] 仓库尚无提交，跳过 worktree 创建，直接在默认分支上工作"
    exit 0
}

# 检查是否已在 worktree 中
$inWorktree = git rev-parse --git-dir 2>$null
if ($inWorktree -match "worktrees") {
    Write-Output "[WORKTREE] 已在 worktree 中，跳过创建"
    exit 0
}

# 获取当前分支名
$branch = git rev-parse --abbrev-ref HEAD 2>$null
if (-not $branch -or $branch -eq "HEAD") {
    Write-Output "[WORKTREE] 无法检测当前分支，跳过创建"
    exit 0
}

# 不在 main/master 上时才创建 worktree
if ($branch -eq "main" -or $branch -eq "master") {
    Write-Output "[WORKTREE] 当前在 $branch 分支，跳过创建（直接在主干上工作）"
    exit 0
}

# 检查是否已有对应的 worktree
$worktreeDir = ".claude/worktrees/$branch"
if (Test-Path $worktreeDir) {
    Write-Output "[WORKTREE] worktree 已存在: $worktreeDir"
    exit 0
}

# 创建 worktree
Write-Output "[WORKTREE] 正在创建隔离工作区: $worktreeDir ..."
mkdir -p ".claude/worktrees" 2>$null
git worktree add "$worktreeDir" "$branch"

Write-Output "[WORKTREE] 隔离工作区已就绪: $projectRoot/$worktreeDir"
Write-Output "[WORKTREE] 请在 $worktreeDir 中进行开发"
