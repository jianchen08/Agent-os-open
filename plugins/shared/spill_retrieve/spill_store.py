# @feature: FP-0.2.spill_guard 取回工具 | @vision: V1 可进化 | @ci: python-plugins-test
"""spill 原文存储的 Python 读侧（与 Rust spill_guard spill_store.rs 契约对齐）。

布局：``{base_path}/{pipeline_id}/{tool_call_id}``——按 pipeline 隔离，管道结束
整目录清理。压缩不靠配置协商：读侧按 gzip magic（``1f 8b``）自动识别解压。

本模块纯函数、无 SDK 依赖，便于直接单测。
"""

from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path
from typing import Any

_GZIP_MAGIC = b"\x1f\x8b"


def sanitize_key(key: str) -> str:
    """key 消毒：仅保留 [A-Za-z0-9._-]，其余替换为 ``_``；连续点号打散。

    与 Rust 侧 ``spill_store::sanitize_key`` 同规则（防 ``../`` 穿越 / 分隔符
    注入 / 父目录字面量）。全非法输入退化为 ``spill_<len>`` 稳定输出。
    """
    sanitized = "".join(
        c if (c.isascii() and (c.isalnum() or c in "_-.")) else "_" for c in key
    )
    while ".." in sanitized:
        sanitized = sanitized.replace("..", "_.")
    if not sanitized.strip("._"):
        return f"spill_{len(key)}"
    return sanitized


def resolve_base_path(configured: str) -> Path:
    """解析 spill 基准目录。

    优先级（与 Rust 侧一致）：
    1. 环境变量 ``AGENTOS_SPILL_BASE``（显式部署控制）
    2. 绝对路径直通
    3. 相对路径锚定项目根——sidecar 进程 cwd 不可靠，从本文件位置向上找
       含 ``config/`` + ``plugins/`` 的目录（内核侧则锚定进程 cwd = 项目根，
       两端在标准部署下解析到同一路径）。
    """
    explicit = os.environ.get("AGENTOS_SPILL_BASE", "").strip()
    if explicit:
        return Path(explicit)
    p = Path(configured)
    if p.is_absolute():
        return p
    root = _infer_project_root()
    return (root / p) if root else Path.cwd() / p


def _infer_project_root() -> Path | None:
    """从本文件位置向上推导项目根（含 config/ + plugins/ 的目录）。"""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "config").is_dir() and (parent / "plugins").is_dir():
            return parent
    return None


def read_spill(base_path: Path | str, pipeline_id: str, tool_call_id: str) -> dict[str, Any]:
    """读回 spill 原文。返回 ``{found, tool_call_id, content, encoding, size_bytes}``。

    文件不存在/读取失败 → ``found=False`` + ``error``（工具层转失败结果，不崩溃）。
    """
    base = Path(base_path)
    path = base / sanitize_key(pipeline_id) / sanitize_key(tool_call_id)
    if not path.is_file():
        return {
            "found": False,
            "tool_call_id": tool_call_id,
            "error": f"spill 原文不存在: {path}",
        }
    try:
        raw = path.read_bytes()
    except OSError as e:
        return {"found": False, "tool_call_id": tool_call_id, "error": f"spill 读取失败: {e}"}
    if raw[:2] == _GZIP_MAGIC:
        try:
            content = gzip.decompress(raw).decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"found": False, "tool_call_id": tool_call_id, "error": f"spill 解压失败: {e}"}
        encoding = "gzip"
    else:
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            return {"found": False, "tool_call_id": tool_call_id, "error": f"spill 原文非 UTF-8: {e}"}
        encoding = "plain"
    return {
        "found": True,
        "tool_call_id": tool_call_id,
        "content": content,
        "encoding": encoding,
        "size_bytes": len(content.encode("utf-8")),
    }


def cleanup_pipeline(base_path: Path | str, pipeline_id: str) -> int:
    """删除该 pipeline 的整个 spill 目录，返回删除的文件数（幂等）。"""
    base = Path(base_path)
    pipe_dir = base / sanitize_key(pipeline_id)
    if not pipe_dir.exists():
        return 0
    count = sum(1 for p in pipe_dir.rglob("*") if p.is_file())
    shutil.rmtree(pipe_dir, ignore_errors=True)
    return count
