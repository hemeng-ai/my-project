#!/usr/bin/env bash
# SessionEnd 自动化收尾脚本（bash 版，macOS / Linux / 云端用）
# 与 session-end.ps1 做同一件事：自动提交 → 运行验证 → 清理 worktree → 输出摘要。
# 用法：bash .claude/hooks/session-end.sh
# （settings.json 里默认配的是 PowerShell 版；在非 Windows 上把
#  那条命令换成上面这条，并先 chmod +x 本文件。）
# 注意：本脚本不重启 dev server（那是 sports-injury-platform 在
# Windows 上的专属行为），需要的话手动启动。
set -u
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_ROOT" || exit 0

SUMMARY=()

# ============================================================
# Step 1: 检测并自动提交未提交变更
# ============================================================
echo "[SessionEnd] 检测未提交变更..."
STATUS="$(git status --porcelain 2>/dev/null || true)"

if [ -n "$STATUS" ]; then
  echo "[SessionEnd] 发现未提交变更，正在自动提交..."
  git add -A 2>/dev/null
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
  COMMIT_MSG="chore: 会话结束自动提交 — $TIMESTAMP — $BRANCH 分支"
  if git commit -m "$COMMIT_MSG" 2>/dev/null; then
    SUMMARY+=("[OK] 已自动提交: $COMMIT_MSG")
    echo "[SessionEnd] 提交成功"
  else
    SUMMARY+=("[警告] 自动提交失败，请手动检查")
    echo "[SessionEnd] 提交失败"
  fi
else
  SUMMARY+=("[跳过] 无未提交变更")
  echo "[SessionEnd] 工作区干净，无需提交"
fi

# ============================================================
# Step 2: 运行验证（lint + test）
# ============================================================
echo "[SessionEnd] 运行验证..."
if [ -f "package.json" ]; then
  if grep -q '"lint"' package.json 2>/dev/null; then
    echo "[SessionEnd] 运行 lint..."
    if npm run lint >/dev/null 2>&1; then SUMMARY+=("[OK] Lint 通过"); else SUMMARY+=("[警告] Lint 存在警告/错误"); fi
  else
    SUMMARY+=("[跳过] 未配置 lint 脚本")
  fi
  if grep -q '"test"' package.json 2>/dev/null; then
    echo "[SessionEnd] 运行测试..."
    if npm test >/dev/null 2>&1; then SUMMARY+=("[OK] 测试通过"); else SUMMARY+=("[警告] 测试存在失败"); fi
  else
    SUMMARY+=("[跳过] 未配置 test 脚本")
  fi
elif [ -f "pytest.ini" ] || [ -f "pyproject.toml" ] || [ -d "tests" ]; then
  echo "[SessionEnd] 运行测试 (pytest)..."
  if python3 -m pytest -q >/dev/null 2>&1; then SUMMARY+=("[OK] 测试通过"); else SUMMARY+=("[警告] 测试存在失败"); fi
else
  SUMMARY+=("[跳过] 未找到 package.json / pytest 配置，跳过验证")
fi

# ============================================================
# Step 3: 清理已合并的 worktree
# ============================================================
echo "[SessionEnd] 清理 worktree..."
MAIN_BRANCH="$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|.*/||')"
MAIN_BRANCH="${MAIN_BRANCH:-main}"
CLEANED=0
while IFS= read -r line; do
  case "$line" in
    *.claude/worktrees/*)
      WT_PATH="$(echo "$line" | awk '{print $1}')"
      WT_FULL="$PROJECT_ROOT/$WT_PATH"
      if [ -d "$WT_FULL" ]; then
        WT_BRANCH="$(git -C "$WT_FULL" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
        if [ -n "$WT_BRANCH" ] && git branch --merged "$MAIN_BRANCH" 2>/dev/null | grep -q "$WT_BRANCH"; then
          echo "[SessionEnd] 清理已合并的 worktree: $WT_PATH (分支: $WT_BRANCH)"
          if git worktree remove "$WT_FULL" --force 2>/dev/null; then
            SUMMARY+=("[OK] 已清理 worktree: $WT_BRANCH")
            CLEANED=1
          else
            SUMMARY+=("[跳过] 无法清理 worktree: $WT_BRANCH")
          fi
        fi
      fi
      ;;
  esac
done <<EOF
$(git worktree list 2>/dev/null)
EOF
if [ "$CLEANED" = "0" ]; then
  SUMMARY+=("[跳过] 无 worktree 需要清理")
fi

# ============================================================
# Step 4: 输出摘要
# ============================================================
echo ""
echo "============================================"
echo " SessionEnd 收尾摘要"
echo "============================================"
for line in "${SUMMARY[@]}"; do
  echo "  $line"
done
echo "============================================"
