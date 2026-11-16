"""git 子进程公共件 —— file 通道（T2）与手动备份（T6）共用的 git 原语。

抽取自 file_channel 的调用机制（台账 R16 设计小节②：裸仓库+分离 work-tree），
T6 手动备份按任务书口径复用同源原语（抽公共件，不整段复制）。约定不变：

- **返回码即结果面**：非零返回码不抛（调用方逐点裁决 stderr）；仅
  OSError/超时翻译为 :class:`GitCommandError`（可执行缺失/进程失败面）。
- **裸仓库 + 分离 work-tree**：``--git-dir/--work-tree`` git 内置机制；
  ``ensure_bare_repo`` 幂等初始化（分支对齐 + 提交身份 + info/exclude 排除面，
  git 内置零源树污染）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

#: 运行时/依赖产物排除（仓库 info/exclude；工作空间复制面同律跳过）。
RUNTIME_EXCLUDES: tuple[str, ...] = (
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "venv/",
    "node_modules/",
    "target/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "*.egg-info/",
    ".git/",
)

DEFAULT_GIT_EXE = "git"
DEFAULT_TIMEOUT_SECS = 120.0


class GitCommandError(RuntimeError):
    """git 子进程执行失败（可执行缺失/进程级故障；非零返回码不在此列）。"""


def run_git(
    git_exe: str,
    git_dir: Path | None,
    work_tree: Path | None,
    *args: str,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    binary: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    """执行单条 git 命令（裸仓库/分离 work-tree 形态按需注入）。

    非零返回码是结果面不抛；OSError（可执行缺失等）与超时翻译为
    :class:`GitCommandError`。``env`` 给定时整体替换子进程环境（如
    GIT_INDEX_FILE 隔离索引），缺省继承当前进程。
    """
    cmd = [git_exe]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    if work_tree is not None:
        cmd += ["--work-tree", str(work_tree)]
    cmd += list(args)
    try:
        return subprocess.run(  # noqa: PLW1510 —— 返回码即结果面，调用方逐点裁决
            cmd,
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            cwd=str(work_tree) if work_tree is not None else None,
            timeout=timeout_secs,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommandError(f"git 执行失败 ({args[0]}): {exc}") from exc


def ensure_bare_repo(
    git_dir: Path, head_ref: str, identity: str, excludes: tuple[str, ...]
) -> None:
    """裸仓库初始化（幂等）：分支对齐 + 本端提交身份 + 排除面。

    排除面写仓库 ``info/exclude``（git 内置机制，零源树污染）。
    """
    if (git_dir / "HEAD").exists():
        return
    try:
        git_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GitCommandError(f"仓库目录创建失败: {exc}") from exc
    init = run_git(DEFAULT_GIT_EXE, None, None, "init", "--bare", "--quiet", str(git_dir))
    if init.returncode != 0:  # pragma: no cover —— 目录创建成功后 git init 不失败（防御分支）
        raise GitCommandError(f"仓库初始化失败: {init.stderr.strip()[:200]}")
    run_git(DEFAULT_GIT_EXE, git_dir, None, "symbolic-ref", "HEAD", head_ref)
    for key, value in (
        ("user.name", identity),
        ("user.email", f"{identity}@agentos.sync"),
        ("core.autocrlf", "false"),
    ):
        run_git(DEFAULT_GIT_EXE, git_dir, None, "config", key, value)
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join(excludes) + "\n", encoding="utf-8")


def stderr_of(proc: subprocess.CompletedProcess[Any]) -> str:
    """非零返回码裁决用的 stderr 摘要（文本形态回读，二进制形态解码兜底）。"""
    raw = cast("subprocess.CompletedProcess[str]", proc).stderr
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
