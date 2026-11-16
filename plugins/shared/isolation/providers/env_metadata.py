"""环境 metadata 档读写（原生无常驻后端的 D6 档真相单源）。

wsl_native / linux_native 两后端的环境模型同为「workspace 目录 + metadata 档」：
档 = ``state_dir/<env名>.json``（workspace 路径 + 创建时间），是收养/验活/按名
销毁的底层真相；档不存在即环境不存在（无常驻进程可删，删档即真删）。

IO 纪律（两后端共用，行为契约由各自测试钉死）：
- 读：缺失返回 None；损坏（非 JSON/非 dict）清档返回 None（坏档 ≠ 存在）；
- 写：OSError 如实 False（调用方落 error env）；
- 删：不存在 = 幂等成功（D6③ 不是谎报——该后端的「底层存在」就是一份档
  文件，档没了就没了）；IO 失败如实 False，保留登记供重试。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# metadata 档文件名后缀（state_dir/<env名>.json）
_METADATA_SUFFIX = ".json"


def metadata_path(state_dir: str | Path, name: str) -> Path:
    return Path(state_dir) / f"{name}{_METADATA_SUFFIX}"


def read_metadata(
    state_dir: str | Path,
    name: str,
    *,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """读 metadata 档；缺失返回 None，损坏清档返回 None（坏档≠存在）。"""
    path = metadata_path(state_dir, name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        logger.warning("metadata 损坏，清除 | name=%s", name)
        remove_metadata(state_dir, name, logger=logger)
        return None


def write_metadata(
    state_dir: str | Path,
    name: str,
    payload: dict[str, Any],
    *,
    logger: logging.Logger,
) -> bool:
    path = metadata_path(state_dir, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError as e:
        logger.error("metadata 写入失败 | name=%s | error=%s", name, e)
        return False


def remove_metadata(
    state_dir: str | Path,
    name: str,
    *,
    logger: logging.Logger,
) -> bool:
    """删档：不存在 = 幂等成功；IO 失败如实 False（不谎报）。"""
    path = metadata_path(state_dir, name)
    if not path.exists():
        return True
    try:
        path.unlink()
        return True
    except OSError as e:
        logger.error("metadata 删除失败 | name=%s | error=%s", name, e)
        return False
