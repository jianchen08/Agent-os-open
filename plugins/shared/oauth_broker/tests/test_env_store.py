# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""``env_store`` 用户根 .env 读写与键名派生测试。

验收面（硬约束「不得清掉别人的键」的机械面）：
- 合并写：既有键原行更新、新键追加、注释与无关键逐字节保留、
  重复同名键只收敛最后一条（对齐内核 ``write_env_updates`` 行级语义）；
- 原子性：不留 ``.tmp`` 残骸，写失败上抛且目标保持旧内容；
- 读面：去引号 / 跳注释空行（对齐内核 ``parse_env_text``）；
- 键名派生：稳定、规范化、合法 env 键面；
- 状态提取：只回键名与到期判定，绝不回 token 值。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from env_store import derive_env_prefix, key_name, merge_env_updates, read_env, status_entries

pytestmark = pytest.mark.unit

PREFIX = "MCP_OAUTH_MCP_EXAMPLE_COM_MCP"


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        ("https://mcp.example.com/mcp", PREFIX),
        ("https://mcp.example.com/mcp/", PREFIX),
        ("https://MCP.Example.com/mcp", PREFIX),
        ("http://127.0.0.1:8123/sse", "MCP_OAUTH_127_0_0_1_8123_SSE"),
        ("https://api.example.com/v1/tools/mcp", "MCP_OAUTH_API_EXAMPLE_COM_V1_TOOLS_MCP"),
    ],
)
def test_derive_env_prefix_normalizes_and_stays_stable(server_url, expected) -> None:
    prefix = derive_env_prefix(server_url)

    assert prefix == expected
    assert prefix == derive_env_prefix(server_url), "同 URL 幂等"
    assert prefix.replace("_", "").isalnum(), f"env 键面只含字母数字下划线：{prefix}"
    assert prefix.upper() == prefix, f"env 键面全大写：{prefix}"


def test_key_name_composes_suffix() -> None:
    assert key_name(PREFIX, "ACCESS_TOKEN") == f"{PREFIX}_ACCESS_TOKEN"
    assert key_name(PREFIX, "REFRESH_TOKEN") == f"{PREFIX}_REFRESH_TOKEN"


def test_merge_preserves_foreign_keys_comments_and_updates_in_place(env_path) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        "# 手工注释\n"
        "OPENAI_API_KEY=sk-other\n"
        f"{PREFIX}_ACCESS_TOKEN=AT-old\n"
        f"{PREFIX}_ACCESS_TOKEN=AT-older\n"
        'KEEP_ME="a b # c"\n',
        encoding="utf-8",
    )
    before = read_env(env_path)

    merge_env_updates(env_path, [(f"{PREFIX}_ACCESS_TOKEN", "AT-new"), (f"{PREFIX}_REFRESH_TOKEN", "RT-1")])

    text = env_path.read_text(encoding="utf-8")
    after = read_env(env_path)
    assert "# 手工注释" in text
    assert after["KEEP_ME"] == "a b # c"
    # 性质：除被更新/新增键外，其余键值一个不差（"不得清掉别人的键"）
    updated = {f"{PREFIX}_ACCESS_TOKEN", f"{PREFIX}_REFRESH_TOKEN"}
    assert {k: v for k, v in after.items() if k not in updated} == {k: v for k, v in before.items() if k not in updated}
    assert after[f"{PREFIX}_ACCESS_TOKEN"] == "AT-new"
    assert after[f"{PREFIX}_REFRESH_TOKEN"] == "RT-1"
    assert text.count(f"{PREFIX}_ACCESS_TOKEN=") == 2, "既有重复行原样保留，只更新最后一条"
    assert not env_path.with_name(env_path.name + ".tmp").exists(), "不留 tmp 残骸"


def test_merge_creates_parent_dirs_and_file(env_path) -> None:
    assert not env_path.exists()

    merge_env_updates(env_path, [(f"{PREFIX}_ACCESS_TOKEN", "AT-1")])

    assert read_env(env_path) == {f"{PREFIX}_ACCESS_TOKEN": "AT-1"}


def test_merge_quotes_values_with_spaces_or_hash(env_path) -> None:
    merge_env_updates(env_path, [("QUOTED", "a b#c")])

    assert 'QUOTED="a b#c"' in env_path.read_text(encoding="utf-8")
    assert read_env(env_path)["QUOTED"] == "a b#c"


def test_merge_failure_propagates_and_keeps_old_content(env_path) -> None:
    """写失败（目标被同名目录占位）→ OSError 上抛，别的 .env 不被截断。"""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("KEEP=1\n", encoding="utf-8")
    blocked = env_path.parent / "blocked.env"
    blocked.mkdir()

    with pytest.raises(OSError, match="blocked"):  # 错误带目标路径，便于定位
        merge_env_updates(blocked, [("A", "1")])

    assert read_env(env_path) == {"KEEP": "1"}
    assert not (env_path.parent / "blocked.env.tmp").exists()


def test_read_env_skips_comments_and_strips_quotes(env_path) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text('# c\n\nA=1\nB="x y"\n  C = z  \nINVALID\n=orphan\n', encoding="utf-8")

    assert read_env(env_path) == {"A": "1", "B": "x y", "C": "z"}


def test_merge_empty_value_removes_key_like_kernel(tmp_path) -> None:
    """空值 = 删键（内核 write_env_updates 同语义）；不存在的键空值是 no-op。"""
    env = tmp_path / ".env"
    env.write_text("KEEP=1\nDROP=2\n", encoding="utf-8")

    merge_env_updates(env, [("DROP", ""), ("ABSENT", "")])

    assert read_env(env) == {"KEEP": "1"}
    assert "ABSENT" not in env.read_text(encoding="utf-8")


def test_status_entries_treats_naive_expiry_as_utc(env_path) -> None:
    """无时区的到期时间按 UTC 解读（内核写面产出的 ISO 串带 +00:00，手写裸串也认）。"""
    moment = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    merge_env_updates(
        env_path,
        [
            (key_name(PREFIX, "ACCESS_TOKEN"), "AT-1"),
            (key_name(PREFIX, "EXPIRES_AT"), "2026-10-17T11:00:00"),
        ],
    )

    (entry,) = status_entries(read_env(env_path), now=moment)

    assert entry["expired"] is True


def test_read_env_missing_file_is_empty(env_path) -> None:
    assert read_env(env_path) == {}


def test_status_entries_report_presence_and_expiry_without_values(env_path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    other = "MCP_OAUTH_OTHER_HOST_MCP"
    merge_env_updates(
        env_path,
        [
            (key_name(PREFIX, "ACCESS_TOKEN"), "AT-secret-1"),
            (key_name(PREFIX, "REFRESH_TOKEN"), "RT-secret-1"),
            (key_name(PREFIX, "EXPIRES_AT"), (now - timedelta(minutes=5)).isoformat()),
            (key_name(PREFIX, "RESOURCE"), "https://mcp.example.com/mcp"),
            (key_name(other, "ACCESS_TOKEN"), "AT-secret-2"),
            (key_name(other, "EXPIRES_AT"), (now + timedelta(minutes=30)).isoformat()),
            (key_name(other, "RESOURCE"), "https://other.host/mcp"),
        ],
    )

    entries = {entry["env_prefix"]: entry for entry in status_entries(read_env(env_path), now=now)}

    assert set(entries) == {PREFIX, other}
    expired = entries[PREFIX]
    assert expired["has_access_token"] is True
    assert expired["has_refresh_token"] is True
    assert expired["expired"] is True
    assert expired["needs_refresh"] is True
    assert expired["resource"] == "https://mcp.example.com/mcp"
    fresh = entries[other]
    assert fresh["expired"] is False
    assert fresh["has_refresh_token"] is False
    assert fresh["needs_refresh"] is False


def test_status_entries_filter_by_prefix_and_unknown_server(env_path) -> None:
    merge_env_updates(env_path, [(key_name(PREFIX, "ACCESS_TOKEN"), "AT-1")])

    assert status_entries(read_env(env_path), prefix=PREFIX)[0]["env_prefix"] == PREFIX
    assert status_entries(read_env(env_path), prefix="MCP_OAUTH_UNKNOWN") == []


def test_status_entries_ignore_unrelated_keys_and_unparseable_expiry(env_path) -> None:
    merge_env_updates(
        env_path,
        [
            ("OPENAI_API_KEY", "sk-x"),
            (key_name(PREFIX, "ACCESS_TOKEN"), "AT-1"),
            (key_name(PREFIX, "EXPIRES_AT"), "not-a-date"),
        ],
    )

    (entry,) = status_entries(read_env(env_path))

    assert entry["env_prefix"] == PREFIX
    assert entry["expired"] is None, "不可解析的到期时间如实置 null（不猜测）"
    assert entry["expires_at"] == "not-a-date"
    assert entry["needs_refresh"] is False
