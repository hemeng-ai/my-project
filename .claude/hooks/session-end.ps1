# SessionEnd 自动化收尾脚本
# 触发时机：Claude Code 会话结束时
# 功能：自动提交 → 运行验证 → 清理 worktree → 重启 dev server → 输出摘要

$ErrorActionPreference = "Continue"
$projectRoot = $env:CLAUDE_PROJECT_DIR
if (-not $projectRoot) { $projectRoot = (Get-Location).Path }
Set-Location $projectRoot

$summary = @()

# ============================================================
# Step 1: 检测并自动提交未提交变更
# ============================================================
Write-Output "[SessionEnd] 检测未提交变更..."
$status = git status --porcelain 2>$null

if ($status) {
    Write-Output "[SessionEnd] 发现未提交变更，正在自动提交..."
    git add -A 2>&1 | Out-Null

    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    if (-not $branch) { $branch = "unknown" }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $commitMsg = "chore: 会话结束自动提交 — $timestamp — $branch 分支"

    git commit -m $commitMsg 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $summary += "[OK] 已自动提交: $commitMsg"
        Write-Output "[SessionEnd] 提交成功"
    } else {
        $summary += "[警告] 自动提交失败，请手动检查"
        Write-Output "[SessionEnd] 提交失败"
    }
} else {
    $summary += "[跳过] 无未提交变更"
    Write-Output "[SessionEnd] 工作区干净，无需提交"
}

# ============================================================
# Step 2: 运行验证（lint + test）
# ============================================================
Write-Output "[SessionEnd] 运行验证..."

if (Test-Path "package.json") {
    $packageJson = Get-Content "package.json" | ConvertFrom-Json
    $scripts = $packageJson.scripts

    # 运行 lint（如存在）
    if ($scripts.PSObject.Properties.Name -contains "lint") {
        Write-Output "[SessionEnd] 运行 lint..."
        npm run lint 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $summary += "[OK] Lint 通过"
        } else {
            $summary += "[警告] Lint 存在警告/错误"
        }
    } else {
        $summary += "[跳过] 未配置 lint 脚本"
    }

    # 运行 test（如存在）
    if ($scripts.PSObject.Properties.Name -contains "test") {
        Write-Output "[SessionEnd] 运行测试..."
        npm test 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $summary += "[OK] 测试通过"
        } else {
            $summary += "[警告] 测试存在失败"
        }
    } else {
        $summary += "[跳过] 未配置 test 脚本"
    }
} else {
    $summary += "[跳过] 未找到 package.json，跳过验证"
}

# ============================================================
# Step 3: 清理已合并的 worktree
# ============================================================
Write-Output "[SessionEnd] 清理 worktree..."

$worktreeList = git worktree list 2>$null
if ($worktreeList) {
    $mainBranch = (git symbolic-ref refs/remotes/origin/HEAD 2>$null) -replace ".*/", ""
    if (-not $mainBranch) { $mainBranch = "main" }

    $lines = $worktreeList -split "`n"
    foreach ($line in $lines) {
        if ($line -match "(.claude/worktrees/\S+)" -and $line -notmatch "\[") {
            $wtPath = $Matches[1]
            $wtFullPath = Join-Path $projectRoot $wtPath
            if (Test-Path $wtFullPath) {
                # 检查该 worktree 的分支是否已合并到 main
                $wtBranch = git -C $wtFullPath rev-parse --abbrev-ref HEAD 2>$null
                if ($wtBranch) {
                    $merged = git branch --merged $mainBranch 2>$null | Select-String $wtBranch
                    if ($merged) {
                        Write-Output "[SessionEnd] 清理已合并的 worktree: $wtPath (分支: $wtBranch)"
                        git worktree remove "$wtFullPath" --force 2>&1 | Out-Null
                        if ($LASTEXITCODE -eq 0) {
                            $summary += "[OK] 已清理 worktree: $wtBranch"
                        } else {
                            $summary += "[跳过] 无法清理 worktree: $wtBranch"
                        }
                    }
                }
            }
        }
    }
} else {
    $summary += "[跳过] 无 worktree 需要清理"
}

# ============================================================
# Step 4: 杀掉旧 dev server + 重启
# ============================================================
Write-Output "[SessionEnd] 重启开发服务器..."

$sipDir = Join-Path $projectRoot "sports-injury-platform"
if (Test-Path $sipDir) {
    # 杀掉占用 3000 端口的进程
    $procId = (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
    if ($procId) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Output "[SessionEnd] 已停止旧 dev server (PID: $procId)"
        $summary += "[OK] 已停止旧 dev server"
    }

    # 后台启动新 dev server
    $devLog = Join-Path $sipDir ".next\dev-server.log"
    Start-Process powershell -ArgumentList "-NoProfile -WindowStyle Hidden -Command `"cd '$sipDir'; npx next dev --port 3000 *>&1 | Out-File '$devLog'`""
    Write-Output "[SessionEnd] 新 dev server 已后台启动，等待就绪..."

    # 等待服务就绪（最长 30 秒）
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -Uri "http://localhost:3000" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($response.StatusCode -eq 200 -or $response.StatusCode -eq 304) {
                $ready = $true
                break
            }
        } catch {
            # 继续等待
        }
    }

    if ($ready) {
        $summary += "[OK] Dev server 已就绪: http://localhost:3000"
        Write-Output "[SessionEnd] Dev server 就绪"
    } else {
        $summary += "[提示] Dev server 启动中，请稍后访问 http://localhost:3000"
        Write-Output "[SessionEnd] Dev server 仍在启动中"
    }
} else {
    $summary += "[跳过] 未找到 sports-injury-platform 目录"
}

# ============================================================
# Step 5: 输出摘要
# ============================================================
Write-Output ""
Write-Output "============================================"
Write-Output " SessionEnd 收尾摘要"
Write-Output "============================================"
foreach ($line in $summary) {
    Write-Output "  $line"
}
Write-Output "============================================"
Write-Output ""
Write-Output " 验证地址: http://localhost:3000"
Write-Output "============================================"
