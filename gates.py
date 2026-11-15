"""
gates.py — 真实门禁：测试 / lint / git diff

P1 新增。只做“跑命令、读结果、分类”，不做任何模型调用，
也不修改项目文件。所有函数都是纯工具函数，便于单测。

设计说明：
- run_tests 会自动检测测试框架：pytest → jest → go test → npm test，
  都检测不到时返回 skipped（不是 fail），避免在没有测试的仓库里
  把流水线卡死。
- classify_test_failure 用于 TDD 红灯校验：红灯只能来自“未实现”
  （断言失败 / NotImplementedError），来自语法错误或环境问题
  （SyntaxError / ImportError / ModuleNotFoundError）时按门禁
  失败处理，交给人工介入而不是 fix-agent（fix-agent 不改测试）。
- run_lint 是 best-effort：没有配置 linter 时返回 skipped；
  配置了但失败时返回 fail，由 orchestrator 决定是否阻塞。
- 回归判定是 best-effort：用 pytest / jest 输出里解析出的失败
  用例名做集合比较；解析不出来时退化为整体 pass/fail 比较。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT = 600
TAIL_CHARS = 4000

# 语法 / 环境类错误：红灯时出现它们说明测试本身或环境有问题，
# 不是“被测代码未实现”。
_SYNTAX_MARKERS = ("SyntaxError", "IndentationError", "TabError")
_ENV_MARKERS = (
    "ModuleNotFoundError",
    "ImportError",
    "No module named",
    "Cannot find module",
    "command not found",
)


def run_command(cmd, cwd=".", timeout=DEFAULT_TIMEOUT):
    """跑一条命令，返回 {returncode, stdout, stderr, timed_out, output_tail}。

    永远不抛异常：命令不存在、超时、执行失败都以返回值表达，
    由调用方按门禁规则处理。
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            errors="replace",
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "timed_out": False,
            "output_tail": combined[-TAIL_CHARS:],
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        combined = out + err
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "output_tail": (combined or "命令超时")[-TAIL_CHARS:],
        }
    except FileNotFoundError as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "output_tail": f"命令不存在：{exc}",
        }
    except Exception as exc:  # noqa: BLE001 - 门禁工具不抛异常
        return {
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "output_tail": f"执行命令失败：{exc}",
        }


def detect_test_runner(cwd="."):
    """检测测试框架。返回 (runner, reason)，runner 为 None 表示无测试。"""
    root = Path(cwd)
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        dev = {}
        dev.update(data.get("devDependencies") or {})
        dev.update(data.get("dependencies") or {})
        scripts = data.get("scripts") or {}
        if "jest" in dev or "jest" in str(scripts.get("test", "")):
            return "jest", "package.json 声明了 jest"
        if "test" in scripts:
            return "npm", "package.json 有 test 脚本"
    for name in ("pytest.ini", "setup.cfg", "tox.ini"):
        if (root / name).exists():
            return "pytest", f"检测到 {name}"
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if "pytest" in text:
            return "pytest", "pyproject.toml 提到 pytest"
    tests_dir = root / "tests"
    if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
        return "pytest", "tests/ 下有 test_*.py"
    if (root / "go.mod").exists():
        return "go", "检测到 go.mod"
    return None, "未检测到测试框架（无 pytest/jest/go test/npm test）"


def parse_failed_tests(output, runner):
    """从测试输出里 best-effort 解析失败用例名。解析不出来返回 []。"""
    failed = []
    if not output:
        return failed
    if runner == "pytest":
        for line in output.splitlines():
            m = re.match(r"FAILED\s+(\S+)", line.strip())
            if m:
                failed.append(m.group(1))
        return sorted(set(failed))
    if runner in ("jest", "npm"):
        for line in output.splitlines():
            m = re.match(r"\s*[✕×x]\s+(.+)", line.strip())
            if m:
                failed.append(m.group(1).strip())
        return sorted(set(failed))
    return []


def classify_test_failure(output):
    """分类一次测试失败。返回 not-implemented / assertion / syntax /
    environment / other 之一。"""
    text = output or ""
    if any(m in text for m in _SYNTAX_MARKERS):
        return "syntax"
    if any(m in text for m in _ENV_MARKERS):
        return "environment"
    if "NotImplementedError" in text or "not implemented" in text.lower():
        return "not-implemented"
    if "AssertionError" in text or "assert " in text or "FAILED" in text or "✕" in text:
        return "assertion"
    return "other"


def run_tests(cwd=".", timeout=DEFAULT_TIMEOUT):
    """运行测试。返回 {runner, status, returncode, failed_tests,
    failure_kind, output_tail}，status 为 pass / fail / skipped。"""
    runner, reason = detect_test_runner(cwd)
    if runner is None:
        return {
            "runner": None,
            "status": "skipped",
            "returncode": None,
            "failed_tests": [],
            "failure_kind": None,
            "output_tail": reason,
        }
    commands = {
        "pytest": [sys.executable, "-m", "pytest", "-q"],
        "jest": ["npx", "jest", "--ci"],
        "go": ["go", "test", "./..."],
        "npm": ["npm", "test"],
    }
    result = run_command(commands[runner], cwd=cwd, timeout=timeout)
    if result["timed_out"]:
        return {
            "runner": runner,
            "status": "fail",
            "returncode": result["returncode"],
            "failed_tests": [],
            "failure_kind": "environment",
            "output_tail": result["output_tail"],
        }
    if result["returncode"] is None:
        # 命令本身没能启动（如 python/pytest 不存在）：环境问题，不是测试失败。
        return {
            "runner": runner,
            "status": "fail",
            "returncode": None,
            "failed_tests": [],
            "failure_kind": "environment",
            "output_tail": result["output_tail"],
        }
    if result["returncode"] == 0:
        return {
            "runner": runner,
            "status": "pass",
            "returncode": 0,
            "failed_tests": [],
            "failure_kind": None,
            "output_tail": result["output_tail"],
        }
    combined = result["output_tail"]
    return {
        "runner": runner,
        "status": "fail",
        "returncode": result["returncode"],
        "failed_tests": parse_failed_tests(combined, runner),
        "failure_kind": classify_test_failure(combined),
        "output_tail": combined,
    }


def run_lint(cwd="."):
    """Best-effort lint。返回 {tool, status, output_tail}，
    status 为 pass / fail / skipped。没有配置 linter 时返回 skipped。"""
    root = Path(cwd)
    candidates = []
    if (root / "pyproject.toml").exists() or (root / "ruff.toml").exists():
        candidates.append((["ruff", "check", "."], "ruff"))
    if (root / "setup.cfg").exists() or (root / ".flake8").exists():
        candidates.append((["flake8", "."], "flake8"))
    eslint_configs = (".eslintrc", ".eslintrc.json", ".eslintrc.js", "eslint.config.js")
    if any((root / name).exists() for name in eslint_configs):
        candidates.append((["npx", "eslint", "."], "eslint"))
    if not candidates:
        return {"tool": None, "status": "skipped", "output_tail": "未检测到 linter 配置（ruff/flake8/eslint），跳过 lint"}
    cmd, tool = candidates[0]
    result = run_command(cmd, cwd=cwd, timeout=300)
    return {
        "tool": tool,
        "status": "pass" if result["returncode"] == 0 else "fail",
        "output_tail": result["output_tail"],
    }


def get_git_head(cwd="."):
    """当前 git HEAD 的 SHA，失败返回 None。"""
    result = run_command(["git", "rev-parse", "HEAD"], cwd=cwd, timeout=30)
    if result["returncode"] == 0:
        return (result["stdout"] or "").strip() or None
    return None


def get_git_diff_stat(cwd="."):
    """git diff --stat + git status --porcelain 的摘要。"""
    diff = run_command(["git", "diff", "--stat"], cwd=cwd, timeout=30)
    status = run_command(["git", "status", "--porcelain"], cwd=cwd, timeout=30)
    files = []
    for line in (status["stdout"] or "").splitlines():
        parts = line.strip().split()
        if parts:
            files.append(parts[-1])
    return {
        "diff_stat": (diff["stdout"] or "").strip(),
        "changed_files": files,
    }


def get_git_status(cwd="."):
    """工作区是否干净。返回 {clean, changed_files}。"""
    info = get_git_diff_stat(cwd)
    return {"clean": not info["changed_files"], "changed_files": info["changed_files"]}
