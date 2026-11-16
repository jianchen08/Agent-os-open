# @feature: FP-0.2.多端同步 2c 分布式路由 | @ci: python-coverage
"""执行通道挂载面测试（ADR 2c 草案 D10/D11：per-device mcp:external manifest）。

断行为：manifest 契约（mcp:external + streamable_http + bearer env 引用 +
allowed_roots 属地领地声明）、P6（token 明文永不落 manifest）、fail-fast
（exec_channel 缺失 / token env 未设）、写卸对称与目录守卫。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mount import (
    build_mount_manifest,
    exec_token_env,
    mount_plugin_id,
    protocol_handshake_error,
    provision_exec_token_env,
    remove_mount,
    sanitize_device_id,
    write_mount,
)

pytestmark = pytest.mark.unit

TOKEN_ENV = "AGENTOS_EXEC_TOKEN_TEST_DEV"


def _record(allowed_roots: list[str] | None = None, mcp_url: str = "http://10.0.0.9:9201/mcp") -> dict[str, Any]:
    caps: dict[str, Any] = {"exec_channel": {"mcp_url": mcp_url}}
    if allowed_roots is not None:
        caps["exec_channel"]["allowed_roots"] = allowed_roots
    return {"device_id": "DESKTOP-41ST1LM", "tier": "desktop", "capabilities": caps}


@pytest.fixture
def token_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(TOKEN_ENV, "secret-exec-token")
    return TOKEN_ENV


class TestNamingContract:
    def test_sanitize_keeps_safe_chars(self) -> None:
        assert sanitize_device_id("DESKTOP-41ST1LM") == "DESKTOP-41ST1LM"
        assert sanitize_device_id("a b/c*d") == "a_b_c_d"

    def test_plugin_id_and_env_derive_from_device(self) -> None:
        assert mount_plugin_id("DESKTOP-41ST1LM") == "mcp_device_desktop-41st1lm"
        assert exec_token_env("DESKTOP-41ST1LM") == "AGENTOS_EXEC_TOKEN_DESKTOP_41ST1LM"
        assert exec_token_env("a b") == "AGENTOS_EXEC_TOKEN_A_B"


class TestBuildManifest:
    def test_contract_mcp_external_streamable_http_bearer_env_ref(self, token_env: str) -> None:
        m = build_mount_manifest(_record(allowed_roots=["D:\\ws"]), token_env=token_env)
        assert m["id"] == "mcp_device_desktop-41st1lm"
        assert m["entry"] == "mcp:external"
        assert m["plugin_type"] == "tool"
        assert m["language"] == "external"
        mcp = m["mcp"]
        assert mcp["transport"] == "streamable_http"
        assert mcp["endpoint"]["url"] == "http://10.0.0.9:9201/mcp"
        auth = mcp["endpoint"]["auth"]
        assert auth["type"] == "bearer"
        assert auth["value"] == "${AGENTOS_EXEC_TOKEN_TEST_DEV}"
        assert auth["required"] is True
        assert mcp["allowed_roots"] == ["D:\\ws"]

    def test_token_value_never_enters_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P6 性质断言：manifest 全文不含 token 明文（只有 env 引用）。"""
        monkeypatch.setenv(TOKEN_ENV, "super-secret-value-42")
        m = build_mount_manifest(_record(), token_env=TOKEN_ENV)
        assert "super-secret-value-42" not in json.dumps(m)

    def test_missing_exec_channel_rejected(self, token_env: str) -> None:
        with pytest.raises(ValueError, match="exec_channel"):
            build_mount_manifest({"device_id": "x", "capabilities": {}}, token_env=token_env)

    def test_missing_mcp_url_rejected(self, token_env: str) -> None:
        with pytest.raises(ValueError, match="mcp_url"):
            build_mount_manifest(_record(mcp_url=""), token_env=token_env)

    def test_missing_device_id_rejected(self, token_env: str) -> None:
        with pytest.raises(ValueError, match="device_id"):
            build_mount_manifest({"capabilities": {"exec_channel": {"mcp_url": "http://x/mcp"}}}, token_env=token_env)

    def test_empty_token_env_name_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        with pytest.raises(ValueError, match="token_env"):
            build_mount_manifest(_record(), token_env="")

    def test_unset_token_env_fails_fast_at_mount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        with pytest.raises(ValueError, match=TOKEN_ENV):
            build_mount_manifest(_record(), token_env=TOKEN_ENV)

    def test_token_value_bypasses_env_presence_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自动签发路径：凭据体随挂载供给（token_value），env 未设不拒——
        内核 connect 时经 .env overlay 解析 ${ENV} 引用（GAP-4a 同机制）。"""
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        m = build_mount_manifest(_record(), token_env=TOKEN_ENV, token_value="fresh-issued-token")
        assert m["mcp"]["endpoint"]["auth"]["value"] == f"${{{TOKEN_ENV}}}"
        assert "fresh-issued-token" not in json.dumps(m)

    def test_empty_allowed_roots_omits_homeland_declaration(self, token_env: str) -> None:
        m = build_mount_manifest(_record(allowed_roots=[]), token_env=token_env)
        assert "allowed_roots" not in m["mcp"]


class TestProtocolHandshake:
    """挂载时协议版本执法（D10a'：低于最低版/畸形 = 明确报错；未声明 = 不假拒）。"""

    @pytest.mark.parametrize(
        ("declared", "compatible"),
        [("1", True), ("1.2", True), ("2", True), ("0.9", False), ("abc", False), ("", True), (None, True)],
    )
    def test_verdict_parametrized(self, declared: Any, compatible: bool) -> None:
        verdict = protocol_handshake_error(declared, minimum="1")
        if compatible:
            assert verdict is None
        else:
            assert isinstance(verdict, str)
            assert "protocol_version" in verdict

    def test_mismatch_message_names_both_sides(self) -> None:
        message = protocol_handshake_error("0.9", minimum="2")
        assert message is not None
        assert "0.9" in message
        assert "2" in message


class TestProvisionExecTokenEnv:
    """env 供给：用户空间 .env 行级合并（内核 write_env_updates 同语义）。"""

    def test_appends_when_absent(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        out = provision_exec_token_env(env_path, TOKEN_ENV, "tok-1")
        assert out == env_path
        assert env_path.read_text(encoding="utf-8") == f"{TOKEN_ENV}=tok-1\n"

    def test_updates_existing_key_in_place_single_occurrence(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            f"PROVIDER_KEY=abc\n{TOKEN_ENV}=stale\nOTHER=keep\n", encoding="utf-8"
        )
        provision_exec_token_env(env_path, TOKEN_ENV, "fresh")
        lines = env_path.read_text(encoding="utf-8").splitlines()
        assert lines == ["PROVIDER_KEY=abc", f"{TOKEN_ENV}=fresh", "OTHER=keep"]

    def test_preserves_comments_and_blank_lines(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("# 密钥区\n\nEXISTING=1\n", encoding="utf-8")
        provision_exec_token_env(env_path, TOKEN_ENV, "tok")
        text = env_path.read_text(encoding="utf-8")
        assert text.startswith("# 密钥区\n\nEXISTING=1\n")
        assert f"{TOKEN_ENV}=tok" in text

    def test_reissuance_is_idempotent_replace(self, tmp_path: Path) -> None:
        """续用重签：同一 env 名重挂 = 原行更新（短时效轮换不留旧值副本）。"""
        env_path = tmp_path / ".env"
        provision_exec_token_env(env_path, TOKEN_ENV, "tok-v1")
        provision_exec_token_env(env_path, TOKEN_ENV, "tok-v2")
        lines = env_path.read_text(encoding="utf-8").splitlines()
        assert lines == [f"{TOKEN_ENV}=tok-v2"]


class TestWriteRemove:
    def test_write_creates_plugin_json_and_is_idempotent(self, tmp_path: Path, token_env: str) -> None:
        m = build_mount_manifest(_record(), token_env=token_env)
        out = write_mount(tmp_path, m)
        assert out == tmp_path / "mcp_device_desktop-41st1lm"
        on_disk = json.loads((out / "plugin.json").read_text(encoding="utf-8"))
        assert on_disk == m
        write_mount(tmp_path, m)  # 幂等重写不抛
        assert (out / "plugin.json").read_text(encoding="utf-8") == json.dumps(m, ensure_ascii=False, indent=2)

    def test_remove_deletes_mount_and_reports_missing(self, tmp_path: Path, token_env: str) -> None:
        m = build_mount_manifest(_record(), token_env=token_env)
        out = write_mount(tmp_path, m)
        assert remove_mount(tmp_path, "DESKTOP-41ST1LM") is True
        assert not out.exists()
        assert remove_mount(tmp_path, "DESKTOP-41ST1LM") is False

    def test_remove_guards_non_mount_dirs(self, tmp_path: Path) -> None:
        """目录守卫：只动 mcp_device_* 命名空间，其余一律不碰。"""
        stranger = tmp_path / "bash"
        stranger.mkdir()
        (stranger / "plugin.json").write_text("{}", encoding="utf-8")
        assert remove_mount(tmp_path, "bash") is False
        assert stranger.exists()
