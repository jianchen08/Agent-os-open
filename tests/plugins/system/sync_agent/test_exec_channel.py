# @feature: FP-0.2.多端同步 2c 执行通道 | @ci: python-coverage
"""sync_agent exec_channel 端侧面测试（ADR D8/D10a'/D13'）。

断行为：
- exec_channel 能力子键装配（env > settings；未启用 = 空 dict 注册表零噪音）；
- 协议版本握手判定（低于最低版/畸形 = 诚实报错；未声明 = 不假拒）；
- 属主端验证集（拉取注入传输口/持久化续效/constant-time 匹配/时效按注入时钟，
  拉取失败传播且旧集保留——验证面不因拉取失败失明）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 能力子键装配（D8 端侧采集契约）
# ---------------------------------------------------------------------------


def test_capability_assembled_from_settings_block() -> None:
    from exec_channel import EXEC_CHANNEL_PROTOCOL_VERSION, build_capability, exec_channel_from_settings

    config = exec_channel_from_settings(
        {"exec_channel": {"mcp_url": "http://127.0.0.1:9103/mcp", "allowed_roots": ["C:/ws", "C:/ws", "D:/pub"]}},
        env={},
    )
    assert config.enabled is True
    assert build_capability(config) == {
        "mcp_url": "http://127.0.0.1:9103/mcp",
        "allowed_roots": ["C:/ws", "D:/pub"],  # 去空去重保序
        "protocol_version": EXEC_CHANNEL_PROTOCOL_VERSION,
    }


def test_capability_env_overrides_settings_and_parses_comma_roots() -> None:
    from exec_channel import EXEC_ALLOWED_ROOTS_ENV, EXEC_MCP_URL_ENV, build_capability, exec_channel_from_settings

    config = exec_channel_from_settings(
        {"exec_channel": {"mcp_url": "http://settings:1/mcp", "allowed_roots": ["C:/from-settings"]}},
        env={
            EXEC_MCP_URL_ENV: "http://env:2/mcp",
            EXEC_ALLOWED_ROOTS_ENV: "C:/a, C:/b D:/c",
        },
    )
    assert build_capability(config)["mcp_url"] == "http://env:2/mcp"
    assert build_capability(config)["allowed_roots"] == ["C:/a", "C:/b", "D:/c"]


def test_capability_disabled_yields_empty_dict() -> None:
    from exec_channel import build_capability, exec_channel_from_settings

    for settings in ({}, {"exec_channel": {"allowed_roots": ["C:/ws"]}}):
        config = exec_channel_from_settings(settings, env={})
        assert config.enabled is False
        assert build_capability(config) == {}  # 未配置 mcp_url = 不上报，注册表零噪音


def test_capability_roots_non_container_yields_empty() -> None:
    from exec_channel import build_capability, exec_channel_from_settings

    config = exec_channel_from_settings(
        {"exec_channel": {"mcp_url": "http://x/mcp", "allowed_roots": 123}}, env={}
    )
    assert config.enabled is True
    assert build_capability(config)["allowed_roots"] == []  # 形态违约 = 零领地声明


# ---------------------------------------------------------------------------
# 协议版本握手（D10a'：最低协议版本 + 不兼容诚实报）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "compatible"),
    [("1", True), ("1.2", True), ("2", True), ("0.9", False), ("abc", False), ("", True), (None, True)],
)
def test_protocol_version_verdict(declared: Any, compatible: bool) -> None:
    from exec_channel import check_protocol_version

    verdict = check_protocol_version(declared, minimum="1")
    if compatible:
        assert verdict is None
    else:
        assert isinstance(verdict, str)
        assert "protocol_version" in verdict


def test_protocol_version_mismatch_message_names_both_sides() -> None:
    from exec_channel import check_protocol_version

    message = check_protocol_version("0.9", minimum="2")
    assert message is not None
    assert "0.9" in message
    assert "2" in message


# ---------------------------------------------------------------------------
# 属主端验证集（D13' 属主端验证面）
# ---------------------------------------------------------------------------


def _fetched(token: str, *, issued_by: str = "device-a", ttl: timedelta = timedelta(hours=1)) -> list[dict[str, Any]]:
    return [
        {
            "token": token,
            "issued_by": issued_by,
            "expires_at": (NOW + ttl).isoformat(),
        }
    ]


def test_verify_returns_authorization_receipt(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    count = channel.refresh(lambda: _fetched("tok-good"), now_iso=NOW.isoformat())

    assert count == 1
    receipt = channel.verify("tok-good", now=NOW)
    assert receipt == {
        "issued_by": "device-a",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }
    assert channel.verify("tok-wrong", now=NOW) is None
    assert channel.verify("", now=NOW) is None
    assert channel.status()["tokens_persisted"] == 1


def test_verify_expiry_boundary_and_monotone_collapse(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    channel.refresh(lambda: _fetched("tok-ttl", ttl=timedelta(hours=1)), now_iso=NOW.isoformat())

    edge = NOW + timedelta(hours=1)
    assert channel.verify("tok-ttl", now=edge - timedelta(seconds=1)) is not None
    assert channel.verify("tok-ttl", now=edge) is not None  # 压线有效（时效闭区间）
    assert channel.verify("tok-ttl", now=edge + timedelta(seconds=1)) is None
    # 单调坍缩：一旦过期，更晚时刻恒无效（性质断言，防时间倒挂实现）
    assert channel.verify("tok-ttl", now=edge + timedelta(hours=5)) is None


def test_refresh_failure_propagates_and_keeps_old_set(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    channel.refresh(lambda: _fetched("tok-old"), now_iso=NOW.isoformat())

    def _boom() -> list[dict[str, Any]]:
        raise RuntimeError("relay unreachable")

    with pytest.raises(RuntimeError):
        channel.refresh(_boom, now_iso=NOW.isoformat())
    assert channel.verify("tok-old", now=NOW) is not None  # 旧集保留（不失明）
    assert channel.status()["synced_at"] == NOW.isoformat()


def test_refresh_replaces_set_rotation(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    channel.refresh(lambda: _fetched("tok-old"), now_iso=NOW.isoformat())
    channel.refresh(
        lambda: _fetched("tok-new", issued_by="device-c"),
        now_iso=(NOW + timedelta(minutes=1)).isoformat(),
    )
    assert channel.verify("tok-old", now=NOW) is None  # 换集即轮换
    receipt = channel.verify("tok-new", now=NOW)
    assert receipt is not None
    assert receipt["issued_by"] == "device-c"


def test_persisted_set_survives_reopen_without_refresh(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    path = tmp_path / "exec_verification.json"
    channel = ExecTokenChannel(path)
    channel.refresh(lambda: _fetched("tok-persist"), now_iso=NOW.isoformat())

    reopened = ExecTokenChannel(path)  # 模拟属主端重启
    assert reopened.verify("tok-persist", now=NOW) is not None
    assert reopened.status() == channel.status()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"tokens", "synced_at"}


def test_refresh_skips_malformed_entries(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    payload = [
        {"token": "", "issued_by": "device-a", "expires_at": NOW.isoformat()},  # 空 token 丢弃
        {"issued_by": "device-a", "expires_at": NOW.isoformat()},  # 缺 token 丢弃
        "not-a-dict",  # 形态违约丢弃
        *_fetched("tok-kept"),
    ]
    assert channel.refresh(lambda: payload, now_iso=NOW.isoformat()) == 1
    assert channel.verify("tok-kept", now=NOW) is not None


def test_verify_fail_closed_on_malformed_expiry(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    channel.refresh(
        lambda: [{"token": "tok-broken", "issued_by": "device-a", "expires_at": "garbage"}],
        now_iso=NOW.isoformat(),
    )
    assert channel.verify("tok-broken", now=NOW) is None


def test_persisted_toplevel_non_dict_ignored(tmp_path: Path) -> None:
    """持久化文件顶层形态违约 = 视为空集（不崩不误验，fail-closed）。"""
    from exec_channel import ExecTokenChannel

    path = tmp_path / "exec_verification.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    channel = ExecTokenChannel(path)
    assert channel.verify("tok-any", now=NOW) is None
    assert channel.status() == {"tokens_persisted": 0, "synced_at": ""}


def test_verify_naive_expiry_treated_as_utc(tmp_path: Path) -> None:
    from exec_channel import ExecTokenChannel

    channel = ExecTokenChannel(tmp_path / "exec_verification.json")
    naive_live = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    channel.refresh(
        lambda: [{"token": "tok-naive", "issued_by": "device-a", "expires_at": naive_live}],
        now_iso=NOW.isoformat(),
    )
    edge = NOW + timedelta(hours=1)
    assert channel.verify("tok-naive", now=edge) is not None  # 无时区 = UTC 补位，压线有效
    assert channel.verify("tok-naive", now=edge + timedelta(seconds=1)) is None
