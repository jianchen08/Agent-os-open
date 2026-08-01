"""manager 的 CONTAINER 级 provider 选择链测试（TDD / M2）。

契约（见 docs/working/design/bwrap_isolation_migration_plan.md §3.3）：
- _create_providers_from_config 的 CONTAINER 级 provider 选择为 bwrap→docker→host：
  - bwrap 在 PATH 且配置启用 → BwrapProvider
  - 否则 docker 配置启用 → DockerProvider（现有行为，回退）
  - 都不可用 → 不放 CONTAINER（manager 后续降级到 HOST）

本测试锁定选择链的分支正确性，不依赖真实 bwrap/docker。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from isolation.manager import _create_providers_from_config
from isolation.providers.bwrap_provider import BwrapProvider
from isolation.providers.docker_provider import DockerProvider
from isolation.types import IsolationLevel


def _docker_enabled_config() -> dict:
    """最小可用 docker 配置（docker_config.enabled=True）。"""
    return {
        "docker": {"enabled": True, "image": "agentos:latest"},
        "bwrap": {"enabled": True},
    }


# ---------------------------------------------------------------------------
# 1. bwrap 在 PATH → BwrapProvider 胜出
# ---------------------------------------------------------------------------


def test_select_bwrap_when_available():
    """bwrap 在 PATH + 配置启用 → CONTAINER 级选 BwrapProvider。"""
    with patch(
        "isolation.providers.bwrap_provider.shutil.which",
        return_value="/usr/bin/bwrap",
    ):
        providers = _create_providers_from_config(providers_config=_docker_enabled_config())

    container = providers.get(IsolationLevel.CONTAINER)
    assert container is not None
    assert isinstance(container, BwrapProvider)


# ---------------------------------------------------------------------------
# 2. bwrap 不在 PATH → 回退 DockerProvider
# ---------------------------------------------------------------------------


def test_fallback_docker_when_bwrap_unavailable():
    """bwrap 不在 PATH → CONTAINER 级回退 DockerProvider（保留现有行为）。"""
    with patch(
        "isolation.providers.bwrap_provider.shutil.which",
        return_value=None,
    ):
        providers = _create_providers_from_config(providers_config=_docker_enabled_config())

    container = providers.get(IsolationLevel.CONTAINER)
    assert container is not None
    assert isinstance(container, DockerProvider)


# ---------------------------------------------------------------------------
# 3. bwrap 配置禁用 → 即便在 PATH 也用 docker
# ---------------------------------------------------------------------------


def test_docker_when_bwrap_disabled_even_if_available():
    """bwrap 配置 enabled=False → 即使 bwrap 在 PATH 也走 docker。"""
    config = _docker_enabled_config()
    config["bwrap"]["enabled"] = False
    with patch(
        "isolation.providers.bwrap_provider.shutil.which",
        return_value="/usr/bin/bwrap",
    ):
        providers = _create_providers_from_config(providers_config=config)

    container = providers.get(IsolationLevel.CONTAINER)
    assert isinstance(container, DockerProvider)


# ---------------------------------------------------------------------------
# 4. HOST 级不受影响
# ---------------------------------------------------------------------------


def test_host_provider_unaffected():
    """HOST 级 provider 选择不受 bwrap 选择链影响。"""
    with patch(
        "isolation.providers.bwrap_provider.shutil.which",
        return_value="/usr/bin/bwrap",
    ):
        providers = _create_providers_from_config(providers_config=_docker_enabled_config())

    assert IsolationLevel.HOST in providers
