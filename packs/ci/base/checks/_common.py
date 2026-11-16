# @feature: FP-0.2.〇 项目类型 CI 包
"""项目 CI checker 公共件（stdlib-only）：项目根定位、待检文件清单解析。

checker 由 runner 以 cwd=.agentos/ci 子进程运行；checker 自身以 __file__ 反推
项目根（.agentos/ci/checks/x.py → 上三级 = 项目根），不依赖启动目录。
staged 清单经 stdin 传入（NUL 或换行分隔）；无清单（手动直跑）回落 `git ls-files`。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CI_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CI_DIR.parents[1]
_LARGE_STDIN = 8 * 1024 * 1024


def input_files() -> list[str]:
    """待检文件清单：staged（stdin）优先，缺省回落 tracked（git ls-files）。

    git 不可用/非仓库时返回空清单（caller 按空集平凡绿处理——checker 不做
    仓库存在性判断，那是 runner 与钩子层的职责）。
    """
    entries: list[str] = []
    if not sys.stdin.isatty():
        try:
            data = sys.stdin.read()
        except OSError:
            data = ""
        if data:
            parts = data.split("\0") if "\0" in data else data.splitlines()
            entries = [p for p in parts if p.strip()]
    if entries:
        return entries
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    raw = out.stdout.decode("utf-8", "replace")[:_LARGE_STDIN]
    return [p for p in raw.split("\0") if p]


def read_text_limited(rel: str, limit: int = 2 * 1024 * 1024) -> str | None:
    """按项目根相对路径读文本（UTF-8 宽松解码）；缺失/超限返回 None。"""
    path = PROJECT_ROOT / rel
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def to_posix(rel: str) -> str:
    """把清单里的相对路径统一成正斜杠形态（git 输出恒为 posix）。"""
    return rel.replace("\\", "/")


def is_under(rel_posix: str, dirs: list[str]) -> bool:
    """rel 是否落在任一 allow_dir（posix 相对目录）之下或与其相等。"""
    for d_raw in dirs:
        d = d_raw.strip("/")
        if rel_posix == d or rel_posix.startswith(d + "/"):
            return True
    return False
