"""用户根 ``.env`` 读写（token 落库唯一面）与 OAuth 键名派生。

与内核 ``agentos_mcp::env_file`` 同源语义：读侧按 ``parse_env_text``（跳注释/
空行、值去首尾双引号），写侧按 ``write_env_updates``（行级合并——既有键更新
原行、新键追加、空值删键，tmp + rename 原子替换），**不得清掉他人键**。
用户根解析走 SDK ``diagnostics_settings.user_root``（与内核 ``core::user_space``
同源）；``.env`` 一变内核宿主指纹即判过期，MCP 客户端重建后取到新 token。

键面：``MCP_OAUTH_<SLUG>_<SUFFIX>``（SLUG 由服务器 URL 派生；后缀见
:data:`ENV_KEY_SUFFIXES`）。状态面只回键名与到期判定，绝不回 token 值。
"""

# config-write-surface-exempt: 用户根 .env 是 token 落库唯一面——路径经 SDK
# diagnostics_settings.user_root 单源解析（与内核 core::user_space 同源），写侧
# 行级合并不清他人键，属闸认可的用户空间解析器形态，非自拼路径直写。

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENV_PREFIX = "MCP_OAUTH"
#: 单服务器键面（读侧状态扫描按后缀反解前缀）
ENV_KEY_SUFFIXES = (
    "ACCESS_TOKEN",
    "REFRESH_TOKEN",
    "EXPIRES_AT",
    "TOKEN_ENDPOINT",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "RESOURCE",
)

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def env_file_path() -> Path:
    """用户根 ``.env``（密钥是用户资产：落用户空间，不落仓）。"""
    from agentos_plugin_sdk.diagnostics_settings import user_root

    return user_root() / ".env"


def key_name(prefix: str, suffix: str) -> str:
    """``<prefix>_<SUFFIX>``（前缀来自 :func:`derive_env_prefix`）。"""
    return f"{prefix}_{suffix}"


def derive_env_prefix(server_url: str) -> str:
    """由服务器 URL 派生键前缀：``netloc`` + 路径 → 大写字母数字下划线。"""
    parts = urllib.parse.urlsplit(server_url.strip())
    raw = f"{parts.netloc}{parts.path}".rstrip("/")
    slug = _SLUG_RE.sub("_", raw).strip("_").upper()
    return f"{ENV_PREFIX}_{slug}"


def read_env(path: str | Path) -> dict[str, str]:
    """读 ``.env`` 为键值字典（缺失文件 = 空表；其余读错误传播）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    variables: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        variables[key] = value.strip().strip('"')
    return variables


def merge_env_updates(path: str | Path, updates: Iterable[tuple[str, str]]) -> None:
    """行级合并写（语义同内核 ``write_env_updates``）：空值删键、原子替换。

    Raises:
        OSError: 目标不可写（tmp 写失败或 replace 失败）——失败时目标保持旧内容。
    """
    from _root_api import atomic_write_text

    target = Path(path)
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    lines = existing.splitlines()

    for key, value in updates:
        rendered = f'"{value}"' if any(ch in value for ch in (" ", "#", "\t")) else value
        prefix = f"{key}="
        last_index: int | None = None
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped == key or stripped.startswith(prefix):
                last_index = index
        if value == "":
            if last_index is not None:
                lines.pop(last_index)
            continue
        if last_index is None:
            lines.append(f"{key}={rendered}")
        else:
            lines[last_index] = f"{key}={rendered}"

    text = "\n".join(lines)
    if text:
        text += "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, text)


def status_entries(
    variables: dict[str, str], *, prefix: str | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """按 ``MCP_OAUTH_*`` 键面汇总每服务器状态（只回键名/存在性/到期判定）。

    ``prefix`` 给定时只回该服务器（无键 → 空表）；``expired`` 在无到期时间或
    不可解析时为 ``None``（不猜测）。
    """
    moment = now or datetime.now(UTC)
    grouped: dict[str, dict[str, str]] = {}
    for key, value in variables.items():
        if not key.startswith(f"{ENV_PREFIX}_"):
            continue
        for suffix in ENV_KEY_SUFFIXES:
            marker = f"_{suffix}"
            if key.endswith(marker):
                candidate = key[: -len(marker)]
                if candidate.startswith(f"{ENV_PREFIX}_"):
                    grouped.setdefault(candidate, {})[suffix] = value
                break

    entries: list[dict[str, Any]] = []
    for candidate in sorted(grouped):
        if prefix is not None and candidate != prefix:
            continue
        fields = grouped[candidate]
        expires_raw = fields.get("EXPIRES_AT")
        expired = _is_expired(expires_raw, moment)
        entries.append(
            {
                "env_prefix": candidate,
                "resource": fields.get("RESOURCE"),
                "has_access_token": bool(fields.get("ACCESS_TOKEN")),
                "has_refresh_token": bool(fields.get("REFRESH_TOKEN")),
                "expires_at": expires_raw,
                "expired": expired,
                "needs_refresh": expired is True and bool(fields.get("REFRESH_TOKEN")),
            }
        )
    return entries


def _is_expired(raw: str | None, now: datetime) -> bool | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= now
