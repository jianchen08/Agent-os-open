# @feature: FP-0.2.外部生态导入MCP | @ci: python-coverage
"""``ecosystem_apply`` 的 MCP 写面（``apply_mcp``）行为测试。

验收面：源 ``.mcp.json`` 的非 OAuth 条目合并进配置根（同名以源为准、其余条目与
顶层其它键保留、原子写）、OAuth 条目跳过并如实上报、默认不写（零副作用）、
现值配置不可解析/缺 ``mcpServers`` 对象即拒写且原文件字节不变、重放幂等、
凭据零泄漏（摘要/登记册/日志），以及 stdio 与 http 两形态、Claude 顶层形态。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import mcp_config
import pytest
import skill_landing
from claude_fixtures import add_skill_dir, make_claude_plugin, write_mcp as write_claude_mcp
from codex_fixtures import add_skill, make_codex_plugin, skill_md, write_mcp
from codex_translator import translate_codex
from translator_common import TranslationError

pytestmark = pytest.mark.unit

#: 源 .mcp.json 里各条目的凭据/端点串（摘要、登记册、日志三面不得出现）
LOCAL_TOKEN = "tok-local-3f2e1d"
HTTP_SECRET = "secret-http-9b8a"
OAUTH_SECRET = "oauth-secret-77ad"
HTTP_ENDPOINT = "mcp.example.com/mcp"
OAUTH_ENDPOINT = "api.githubcopilot.com/mcp"
STDIO_COMMAND = "local-tools-cli"
SECRETS = (LOCAL_TOKEN, HTTP_SECRET, OAUTH_SECRET, HTTP_ENDPOINT, OAUTH_ENDPOINT, STDIO_COMMAND)


def _provenance(user_root: Path) -> dict:
    return json.loads((user_root / "ecosystem_imports.json").read_text(encoding="utf-8"))


def _record(user_root: Path) -> dict:
    (record,) = _provenance(user_root)["imports"]
    return record


def _config(config_root: Path) -> dict:
    return json.loads((config_root / "mcp.json").read_text(encoding="utf-8"))


def _source_servers(plugin: Path) -> dict:
    return json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def _non_oauth_names(servers: dict) -> set[str]:
    return {name for name, spec in servers.items() if "oauth" not in spec}


def _mcp_plugin(tmp_path: Path, *, name: str = "mcp-plugin", with_skill: bool = True) -> Path:
    """技能 + 三服务器（stdio / http / 带 OAuth 块的 http）的 MIT 插件。"""
    plugin = make_codex_plugin(tmp_path, name=name, license="MIT")
    if with_skill:
        add_skill(plugin, "use-mcp", content=skill_md("use-mcp", "用 MCP 工具"))
    write_mcp(
        plugin,
        {
            "local-tools": {
                "command": STDIO_COMMAND,
                "args": ["serve", "--token", LOCAL_TOKEN],
                "env": {"LOCAL_TOKEN": LOCAL_TOKEN},
            },
            "remote-http": {
                "type": "http",
                "url": f"https://{HTTP_ENDPOINT}",
                "headers": {"Authorization": f"Bearer {HTTP_SECRET}"},
            },
            "github": {
                "type": "http",
                "url": f"https://{OAUTH_ENDPOINT}/",
                "oauth": {"client_id": "cid-1", "client_secret": OAUTH_SECRET},
            },
        },
    )
    return plugin


async def test_apply_mcp_merges_non_oauth_entries_and_reports_skips(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    plugin = _mcp_plugin(tmp_path)

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is True
    mcp = result.output["mcp"]
    assert [(item["name"], item["transport"]) for item in mcp["written"]] == [
        ("local-tools", "stdio"),
        ("remote-http", "http"),
    ]
    (skipped,) = mcp["skipped"]
    assert skipped["name"] == "github"
    assert "OAuth" in skipped["reason"]
    assert "未写入" in skipped["reason"]
    # 性质：写入后的名字集合 == 源非 OAuth 服务器名集合；条目内容逐字段透传（源为准）
    servers = _config(config_root_env)["mcpServers"]
    assert set(servers) == _non_oauth_names(_source_servers(plugin)) == {"local-tools", "remote-http"}
    assert servers["local-tools"]["command"] == STDIO_COMMAND
    assert servers["local-tools"]["env"] == {"LOCAL_TOKEN": LOCAL_TOKEN}
    assert servers["remote-http"]["headers"] == {"Authorization": f"Bearer {HTTP_SECRET}"}
    assert all("oauth" not in spec for spec in servers.values())
    assert _record(user_root_env)["mcp_written"] == ["local-tools", "remote-http"]


async def test_apply_mcp_preserves_foreign_entries_and_replays_idempotent(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    plugin = _mcp_plugin(tmp_path)
    foreign = {"command": "uvx", "args": ["windows-mcp", "serve"]}
    (config_root_env / "mcp.json").write_text(
        json.dumps(
            {
                "note": "手写备注",
                "mcpServers": {"local-tools": {"command": "OLD-COMMAND"}, "windows-mcp": foreign},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert first.success is True
    doc = _config(config_root_env)
    assert doc["note"] == "手写备注"
    assert doc["mcpServers"]["windows-mcp"] == foreign
    assert doc["mcpServers"]["local-tools"]["command"] == STDIO_COMMAND
    # 性质：合并后名字集合 == 现值既有名字 ∪ 源非 OAuth 名字（不删不重复）
    assert set(doc["mcpServers"]) == _non_oauth_names(_source_servers(plugin)) | {"windows-mcp"}
    bytes_after_first = (config_root_env / "mcp.json").read_bytes()

    second = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True, "force": True})

    assert second.success is True
    assert (config_root_env / "mcp.json").read_bytes() == bytes_after_first


@pytest.mark.parametrize(
    "payload",
    ["{ 半截 JSON", '["mcpServers"]', '{"mcpServers": ["not-a-map"]}', '{"other": 1}', ""],
)
async def test_apply_mcp_refuses_invalid_existing_config_keeping_bytes(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool, payload: str
) -> None:
    """现值配置对内核不可用（不可解析 / 缺 mcpServers 对象）= 拒写，原文件一字不动。"""
    plugin = _mcp_plugin(tmp_path)
    target = config_root_env / "mcp.json"
    target.write_text(payload, encoding="utf-8")

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is False
    assert result.error_code == "MCP_CONFIG_INVALID"
    assert target.read_text(encoding="utf-8") == payload
    # 配置面错误不产生半成品：技能与登记册零写入（合并先于技能提交）
    assert not (user_root_env / "skills").exists()
    assert not (user_root_env / "ecosystem_imports.json").exists()


async def test_apply_mcp_oauth_only_writes_nothing(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    plugin = make_codex_plugin(tmp_path, name="oauth-only-plugin", license="MIT")
    add_skill(plugin, "use-mcp", content=skill_md("use-mcp", "用 MCP 工具"))
    write_mcp(plugin, {"github": {"type": "http", "url": f"https://{OAUTH_ENDPOINT}/", "oauth": {"client_id": "cid"}}})

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is True
    assert result.output["mcp"]["written"] == []
    assert [item["name"] for item in result.output["mcp"]["skipped"]] == ["github"]
    assert not (config_root_env / "mcp.json").exists()
    assert _record(user_root_env)["mcp_written"] == []


async def test_apply_defaults_to_no_mcp_write(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    """未显式请求 apply_mcp：配置面零副作用，登记册不记 mcp_written。"""
    plugin = _mcp_plugin(tmp_path)

    result = await apply_tool.execute({"source_dir": str(plugin)})

    assert result.success is True
    assert result.output["mcp"] == {"written": [], "skipped": []}
    assert not (config_root_env / "mcp.json").exists()
    record = _record(user_root_env)
    assert "mcp_written" not in record
    assert record["skills"] == ["use-mcp"]


async def test_apply_mcp_without_source_mcp_json_writes_nothing(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    plugin = make_codex_plugin(tmp_path, name="no-mcp-plugin", license="MIT")
    add_skill(plugin, "plain", content=skill_md("plain", "无 MCP 声明"))

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is True
    assert result.output["mcp"] == {"written": [], "skipped": []}
    assert not (config_root_env / "mcp.json").exists()
    assert _record(user_root_env)["mcp_written"] == []


async def test_apply_with_broken_source_mcp_json_writes_no_config(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    plugin = _mcp_plugin(tmp_path)
    (plugin / ".mcp.json").write_text("{ 半截 JSON", encoding="utf-8")

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is False
    assert result.error_code == "MCP_INVALID"
    assert not (config_root_env / "mcp.json").exists()
    assert not (user_root_env / "skills").exists()


async def test_apply_mcp_leaks_no_credentials_to_summary_ledger_and_logs(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool, caplog: pytest.LogCaptureFixture
) -> None:
    plugin = _mcp_plugin(tmp_path)

    with caplog.at_level(logging.DEBUG):
        result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is True
    dumped = json.dumps(result.output, ensure_ascii=False)
    ledger = (user_root_env / "ecosystem_imports.json").read_text(encoding="utf-8")
    written = (config_root_env / "mcp.json").read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in dumped
        assert secret not in ledger
        assert secret not in caplog.text
    # 写入面真的落了源内容（防止上面的"零出现"因根本没写而空洞）
    for present in (STDIO_COMMAND, LOCAL_TOKEN, HTTP_SECRET, HTTP_ENDPOINT):
        assert present in written
    assert OAUTH_ENDPOINT not in written
    assert OAUTH_SECRET not in written


@pytest.mark.parametrize("bad", ["true", "false", 1, 0])
async def test_apply_rejects_non_boolean_apply_mcp(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool, bad: object
) -> None:
    """非布尔 apply_mcp fail-closed（不静默做真值转换：``"false"`` 转真值即 True）。"""
    plugin = _mcp_plugin(tmp_path)

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": bad})

    assert result.success is False
    assert result.error_code == "OPTION_INVALID"
    assert not (config_root_env / "mcp.json").exists()
    assert not (user_root_env / "skills").exists()


async def test_apply_mcp_reads_claude_top_level_source(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    """Claude Code 顶层形态（省略 mcpServers 包裹层）同语义：非 OAuth 条目照写。"""
    plugin = make_claude_plugin(tmp_path, name="cc-mcp-plugin", license="MIT")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    write_claude_mcp(
        plugin,
        {"local": {"command": STDIO_COMMAND, "args": ["run"]}, "gh": {"url": "https://gh.example/mcp", "oauth": {}}},
        wrapped=False,
    )

    result = await apply_tool.execute({"source_dir": str(plugin), "kind": "claude_code", "apply_mcp": True})

    assert result.success is True
    assert [item["name"] for item in result.output["mcp"]["written"]] == ["local"]
    assert [item["name"] for item in result.output["mcp"]["skipped"]] == ["gh"]
    assert set(_config(config_root_env)["mcpServers"]) == {"local"}


async def test_apply_mcp_without_skills_writes_config_and_registers(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool
) -> None:
    plugin = _mcp_plugin(tmp_path, name="mcp-only-plugin", with_skill=False)

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is True
    assert result.output["skills_written"] == []
    assert set(_config(config_root_env)["mcpServers"]) == {"local-tools", "remote-http"}
    assert _record(user_root_env)["skills"] == []


async def test_apply_mcp_write_failure_reports_and_lands_no_skills(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置写入失败（文件系统故障面）→ 失败信封，技能零落地（合并先于技能提交）。"""
    plugin = _mcp_plugin(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("模拟配置写失败")

    monkeypatch.setattr(mcp_config, "atomic_write_text", boom)

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is False
    assert result.error_code == "MCP_CONFIG_WRITE_FAILED"
    assert "模拟配置写失败" in result.error
    assert not (config_root_env / "mcp.json").exists()
    assert not (user_root_env / "skills").exists()


async def test_apply_skill_staging_failure_reports_mcp_already_written(
    tmp_path: Path, user_root_env: Path, config_root_env: Path, apply_tool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """技能提交在 MCP 写面之后：提交失败时错误信息如实带上「配置已写入」事实。"""
    plugin = _mcp_plugin(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("模拟技能复制失败")

    monkeypatch.setattr(skill_landing.shutil, "copytree", boom)

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is False
    assert result.error_code == "LAND_FAILED"
    assert "MCP 配置已写入" in result.error
    assert set(_config(config_root_env)["mcpServers"]) == {"local-tools", "remote-http"}
    assert not (user_root_env / "skills" / "use-mcp").exists()
    assert not (user_root_env / "ecosystem_imports.json").exists()


def test_resolve_config_root_follows_env(config_root_env: Path) -> None:
    assert mcp_config.resolve_config_root() == config_root_env


def test_resolve_config_root_unresolved_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_config, "resolve_project_root", lambda *_args, **_kwargs: None)

    with pytest.raises(mcp_config.McpConfigError) as excinfo:
        mcp_config.resolve_config_root()

    assert excinfo.value.code == "CONFIG_ROOT_UNRESOLVED"
    assert not (tmp_path / "mcp.json").exists()


@pytest.mark.parametrize("shape", ["<root>/config", "<root>"])
def test_resolve_config_root_follows_pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """内核钉根两种合法形态（值 = ``<根>/config`` 或含 ``config`` 的根本身）都解析到该根。"""
    import project_root

    root = tmp_path / "pinned"
    config = root / "config"
    config.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(config if shape == "<root>/config" else root))
    project_root.reset_cache()
    try:
        assert mcp_config.resolve_config_root() == config
    finally:
        project_root.reset_cache()


async def test_apply_mcp_refuses_env_root_mismatch(
    tmp_path: Path, user_root_env: Path, monkeypatch: pytest.MonkeyPatch, apply_tool
) -> None:
    """env 钉了根、解析结果却不是它（如指向不存在的根）→ 拒写且不回落其他根。

    回落探测会写到**另一个**配置根（真机形态下即仓根配置），与内核按 env 值读取的根分叉
    ——写 A 层读 B 层正是本仓单真值公理要禁的失败模式，故宁拒不猜。
    """
    import project_root

    plugin = _mcp_plugin(tmp_path)
    absent = tmp_path / "absent" / "config"
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(absent))
    project_root.reset_cache()

    result = await apply_tool.execute({"source_dir": str(plugin), "apply_mcp": True})

    assert result.success is False
    assert result.error_code == "CONFIG_ROOT_MISMATCH"
    assert str(absent) in result.error
    assert not absent.exists()
    assert not (user_root_env / "skills").exists()


def test_write_mcp_servers_rejects_broken_source(tmp_path: Path, config_root_env: Path) -> None:
    """源读面错误沿用翻译族错误（同一文件的同一判定，与翻译面同码）。"""
    plugin = _mcp_plugin(tmp_path)
    ir = translate_codex(plugin)
    (plugin / ".mcp.json").write_text("{ 半截 JSON", encoding="utf-8")

    with pytest.raises(TranslationError) as excinfo:
        mcp_config.write_mcp_servers(ir, config_root=config_root_env)

    assert excinfo.value.code == "MCP_INVALID"
    assert not (config_root_env / "mcp.json").exists()
