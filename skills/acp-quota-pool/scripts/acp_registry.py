"""ACP provider 注册表 —— 注册文件（TOML）解析面（fail-closed）。

单一来源：本技能包 ``providers.toml``（随包播种到工作空间，脚本同目录
相对寻址）。配置缺失/损坏/条目残缺一律 ValueError 拒载——注册表损坏
≠ 空注册表，派发面宁可不发不可错发。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_TIMEOUT_SECONDS = 300.0
BACKEND_ACP = "acp"
BACKEND_ONESHOT = "oneshot"
_BACKENDS = (BACKEND_ACP, BACKEND_ONESHOT)
GOAL_PLACEHOLDER = "{goal}"

__all__ = [
    "BACKEND_ACP",
    "BACKEND_ONESHOT",
    "GOAL_PLACEHOLDER",
    "ProviderSpec",
    "load_registry",
    "load_registry_file",
]


@dataclass(frozen=True)
class ProviderSpec:
    """单家 provider 的 spawn 面与路由/记账元数据。"""

    id: str
    name: str
    command: str
    args: tuple[str, ...]
    timeout_seconds: float
    quota_model: str
    cost_class: str
    strength: str
    backend: str = BACKEND_ACP


def _parse_provider(provider_id: str, raw: Any) -> ProviderSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"providers.{provider_id} 条目必须是表，实得 {type(raw).__name__}")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"providers.{provider_id}.command 必须是非空字符串")
    args_raw = raw.get("args", [])
    if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
        raise ValueError(f"providers.{provider_id}.args 必须是字符串数组")
    backend = raw.get("backend", BACKEND_ACP)
    if backend not in _BACKENDS:
        raise ValueError(
            f"providers.{provider_id}.backend 必须是 {'/'.join(_BACKENDS)} 之一，实得 {backend!r}"
        )
    if backend == BACKEND_ONESHOT and not any(GOAL_PLACEHOLDER in arg for arg in args_raw):
        raise ValueError(
            f"providers.{provider_id}.args 必须含 {GOAL_PLACEHOLDER} 占位符"
            f"（oneshot 后端靠它注入派发目标全文）"
        )
    timeout_raw = raw.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout_raw, (int, float)) or isinstance(timeout_raw, bool):
        raise ValueError(f"providers.{provider_id}.timeout_seconds 必须是数字")
    if timeout_raw <= 0:
        raise ValueError(f"providers.{provider_id}.timeout_seconds 必须为正数，实得 {timeout_raw}")
    strings = {
        key: raw.get(key, default)
        for key, default in (
            ("name", provider_id),
            ("quota_model", ""),
            ("cost_class", ""),
            ("strength", ""),
        )
    }
    for key, value in strings.items():
        if not isinstance(value, str):
            raise ValueError(f"providers.{provider_id}.{key} 必须是字符串")
    return ProviderSpec(
        id=provider_id,
        name=strings["name"],
        command=command,
        args=tuple(args_raw),
        timeout_seconds=float(timeout_raw),
        quota_model=strings["quota_model"],
        cost_class=strings["cost_class"],
        strength=strings["strength"],
        backend=backend,
    )


def load_registry(injected: Mapping[str, Any] | None) -> dict[str, ProviderSpec]:
    """解析注册表字典（TOML 解析产物）；任何形态损坏都 ValueError（fail-closed）。"""
    if not isinstance(injected, Mapping):
        raise ValueError("ACP provider 注册表缺失（providers.toml 未找到或不可解析）")
    raw_providers = injected.get("providers")
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ValueError("ACP provider 注册表为空：providers 必须是非空表")
    return {pid: _parse_provider(pid, raw) for pid, raw in raw_providers.items()}


def load_registry_file(path: Path | str) -> dict[str, ProviderSpec]:
    """读注册文件并解析；文件缺失/解析失败传播为 ValueError/OSError（不吞）。"""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return load_registry(data)
