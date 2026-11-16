# @feature: FP-0.2.外部生态导入 A批 | @ci: python-coverage
"""``ecosystem_translate``（Codex 翻译器工具）行为测试。

验收面：插件身份 / 技能清单（frontmatter 真值）/ MCP 清单（transport + OAuth
块 + 安装明示）/ apps 清单（不可移植）/ 许可证闸门结论；只读零副作用；
坏件 fail-closed 显式报错。

≥2 组有区分度夹具：纯技能 MIT（无 MCP/apps）vs MCP+apps Proprietary；坏件族
参数化展开（缺清单 / JSON 破损 / 结构非法）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from codex_fixtures import (
    add_skill,
    file_snapshot,
    load_server_module,
    make_codex_plugin,
    skill_md,
    write_apps,
    write_mcp,
)

pytestmark = pytest.mark.unit

MCP_INSTALL_NOTE = "MCP 服务器需安装后才可用：ecosystem_apply 加 apply_mcp=true 合并进配置根（OAuth 条目本批不写）"


async def test_translate_reports_identity_skills_and_empty_optional_faces(
    mit_skills_plugin: Path, translate_tool
) -> None:
    result = await translate_tool.execute({"source_dir": str(mit_skills_plugin)})

    assert result.success is True
    output = result.output
    assert output["plugin"]["name"] == "superpowers-lite"
    assert output["plugin"]["version"] == "6.3.0"
    assert output["plugin"]["author"] == "Obra"
    assert output["plugin"]["license"] == "MIT"
    assert [s["skill_id"] for s in output["skills"]] == ["brainstorming", "writing-plans"]
    assert [s["description"] for s in output["skills"]] == ["先头脑风暴再动手", "写执行计划"]
    assert output["mcp_servers"] == []
    assert output["apps"] == []
    assert output["notes"] == []
    assert output["license_gate"]["allowed"] is True
    # 性质：技能清单 = 磁盘上带 SKILL.md 的技能目录全集（声明面 == 实物面）
    on_disk = sorted(p.name for p in (mit_skills_plugin / "skills").iterdir() if (p / "SKILL.md").is_file())
    assert sorted(s["skill_id"] for s in output["skills"]) == on_disk


async def test_translate_reports_mcp_apps_oauth_and_license_rejection(
    proprietary_mcp_plugin: Path, translate_tool
) -> None:
    result = await translate_tool.execute({"source_dir": str(proprietary_mcp_plugin), "kind": "codex"})

    assert result.success is True
    output = result.output
    servers = {s["name"]: s for s in output["mcp_servers"]}
    assert servers["github"]["transport"] == "http"
    assert servers["github"]["requires_oauth"] is True
    assert servers["local-tools"]["transport"] == "stdio"
    assert servers["local-tools"]["requires_oauth"] is False
    # 凭据面不入输出（client_secret 只许留在源文件里）
    assert "client_secret" not in json.dumps(output, ensure_ascii=False)
    assert "3f2e1d0c9b8a" not in json.dumps(output, ensure_ascii=False)
    apps = {a["name"]: a for a in output["apps"]}
    assert apps["linear"]["app_id"] == "asdk_app_69a089a326dc8191b32a3f2553f5be2c"
    assert apps["github"]["app_id"] == "connector_76869538009648d5b282a4bb21c3d157"
    assert all(a["portable"] is False for a in output["apps"])
    assert MCP_INSTALL_NOTE in output["notes"]
    assert output["license_gate"]["allowed"] is False
    assert "Proprietary" in output["license_gate"]["reason"]
    # 性质：MCP 清单非空 ⇔ 安装提示在场；apps 非空 ⇔ 不可移植说明在场
    assert bool(output["mcp_servers"]) is (MCP_INSTALL_NOTE in output["notes"])
    assert any("不可移植" in note for note in output["notes"]) is bool(output["apps"])


async def test_translate_is_read_only(mit_skills_plugin: Path, translate_tool) -> None:
    before = file_snapshot(mit_skills_plugin.parent)

    result = await translate_tool.execute({"source_dir": str(mit_skills_plugin)})

    assert result.success is True
    assert file_snapshot(mit_skills_plugin.parent) == before


async def test_translate_rejects_unsupported_kind(mit_skills_plugin: Path, translate_tool) -> None:
    result = await translate_tool.execute({"source_dir": str(mit_skills_plugin), "kind": "claude-code"})

    assert result.success is False
    assert result.error_code == "KIND_UNSUPPORTED"
    assert "claude-code" in result.error


@pytest.mark.parametrize("args", [{}, {"source_dir": "   "}])
async def test_translate_rejects_missing_source_dir(translate_tool, args: dict[str, str]) -> None:
    result = await translate_tool.execute(dict(args))

    assert result.success is False
    assert result.error_code == "SOURCE_NOT_FOUND"


async def test_translate_rejects_missing_directory(tmp_path: Path, translate_tool) -> None:
    result = await translate_tool.execute({"source_dir": str(tmp_path / "nowhere")})

    assert result.success is False
    assert result.error_code == "SOURCE_NOT_FOUND"


@pytest.mark.parametrize(
    ("author", "expected"),
    [({"name": "Obra"}, "Obra"), ("Solo Dev", "Solo Dev")],
)
async def test_translate_author_forms(tmp_path: Path, translate_tool, author, expected: str) -> None:
    """作者声明两形态（``{"name": ...}`` / 裸字符串）归一为名字串。"""
    plugin = make_codex_plugin(tmp_path, name="author-form-plugin")
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "author-form-plugin", "author": author, "license": "MIT"}),
        encoding="utf-8",
    )

    result = await translate_tool.execute({"source_dir": str(plugin)})

    assert result.success is True
    assert result.output["plugin"]["author"] == expected


@pytest.mark.parametrize(
    "content",
    [
        "# 无 frontmatter 的技能\n\n正文\n",
        "---\n# 只有注释的 frontmatter\n---\n\n# 空 frontmatter\n",
        skill_md(None, "没有 name 的技能"),
    ],
)
async def test_translate_skill_id_falls_back_to_dir_name(tmp_path: Path, translate_tool, content: str) -> None:
    """frontmatter 缺失/为空/无 name 键 → skill_id 回落目录名（声明面如实置空）。"""
    plugin = make_codex_plugin(tmp_path, name="fallback-plugin")
    add_skill(plugin, "plain-skill", content=content)

    result = await translate_tool.execute({"source_dir": str(plugin)})

    assert result.success is True
    (skill,) = result.output["skills"]
    assert skill["skill_id"] == "plain-skill"
    assert skill["name"] == ""


async def test_translate_description_still_read_from_declaration(tmp_path: Path, translate_tool) -> None:
    plugin = make_codex_plugin(tmp_path, name="fallback-desc-plugin")
    add_skill(plugin, "plain-skill", content=skill_md(None, "没有 name 的技能"))

    result = await translate_tool.execute({"source_dir": str(plugin)})

    assert result.output["skills"][0]["description"] == "没有 name 的技能"


@pytest.mark.parametrize("layout", ["no-skills-dir", "empty-skills-dir", "stray-entries"])
async def test_translate_accepts_plugin_without_skills(tmp_path: Path, translate_tool, layout: str) -> None:
    plugin = make_codex_plugin(tmp_path, name=f"no-skill-{layout}")
    if layout == "empty-skills-dir":
        (plugin / "skills").mkdir()
    if layout == "stray-entries":
        # 非技能条目（散文件 / 无 SKILL.md 的目录）不构成技能，也不算坏件
        (plugin / "skills").mkdir()
        (plugin / "skills" / "README.md").write_text("# 说明\n", encoding="utf-8")
        (plugin / "skills" / "not-a-skill").mkdir()

    result = await translate_tool.execute({"source_dir": str(plugin)})

    assert result.success is True
    assert result.output["skills"] == []


# ── 坏件族（fail-closed：可读错误 + 机器可读 error_code）───────────────────


def _missing_manifest_dir(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-manifest-missing")
    (plugin / ".codex-plugin" / "plugin.json").unlink()
    return plugin


def _broken_manifest_json(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-manifest-json")
    (plugin / ".codex-plugin" / "plugin.json").write_text("{ 不是 JSON", encoding="utf-8")
    return plugin


def _manifest_without_name(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-manifest-noname")
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"version": "1.0.0", "license": "MIT"}), encoding="utf-8"
    )
    return plugin


def _manifest_not_object(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-manifest-notobject")
    (plugin / ".codex-plugin" / "plugin.json").write_text("[1, 2, 3]", encoding="utf-8")
    return plugin


def _broken_mcp_json(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-mcp-json")
    (plugin / ".mcp.json").write_text("[1, 2", encoding="utf-8")
    return plugin


def _mcp_without_servers_map(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-mcp-noservers")
    (plugin / ".mcp.json").write_text(json.dumps({"servers": {}}), encoding="utf-8")
    return plugin


def _mcp_server_without_endpoint(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-mcp-noendpoint")
    write_mcp(plugin, {"ghost": {"type": "http"}})
    return plugin


def _mcp_server_not_object(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-mcp-nonobject")
    write_mcp(plugin, {"ghost": "https://example.invalid/mcp"})
    return plugin


def _app_without_id(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-app-noid")
    write_apps(plugin, {"linear": {"category": "Work Tracking"}})
    return plugin


def _app_without_apps_map(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-app-nomap")
    (plugin / ".app.json").write_text(json.dumps({"applications": {}}), encoding="utf-8")
    return plugin


def _broken_skill_frontmatter(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-skill-yaml")
    add_skill(plugin, "bad", content="---\nname: [未闭合\n---\n\n正文\n")
    return plugin


def _skill_frontmatter_not_mapping(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-skill-nonmap")
    add_skill(plugin, "bad", content="---\n- 甲\n- 乙\n---\n\n正文\n")
    return plugin


def _skill_not_utf8(tmp_path: Path) -> Path:
    plugin = make_codex_plugin(tmp_path, name="broken-skill-encoding")
    skill_dir = plugin / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe\x00---\nname: bad\n")
    return plugin


_BAD_FIXTURES = {
    "missing-manifest": (_missing_manifest_dir, "MANIFEST_MISSING"),
    "broken-manifest-json": (_broken_manifest_json, "MANIFEST_INVALID"),
    "manifest-not-object": (_manifest_not_object, "MANIFEST_INVALID"),
    "manifest-without-name": (_manifest_without_name, "MANIFEST_INVALID"),
    "broken-mcp-json": (_broken_mcp_json, "MCP_INVALID"),
    "mcp-without-servers-map": (_mcp_without_servers_map, "MCP_INVALID"),
    "mcp-server-without-endpoint": (_mcp_server_without_endpoint, "MCP_INVALID"),
    "mcp-server-not-object": (_mcp_server_not_object, "MCP_INVALID"),
    "app-without-id": (_app_without_id, "APPS_INVALID"),
    "app-without-apps-map": (_app_without_apps_map, "APPS_INVALID"),
    "broken-skill-frontmatter": (_broken_skill_frontmatter, "SKILL_INVALID"),
    "skill-frontmatter-not-mapping": (_skill_frontmatter_not_mapping, "SKILL_INVALID"),
    "skill-not-utf8": (_skill_not_utf8, "SKILL_INVALID"),
}


@pytest.mark.parametrize("case", sorted(_BAD_FIXTURES))
async def test_translate_bad_fixture_fails_closed(tmp_path: Path, translate_tool, case: str) -> None:
    builder, expected_code = _BAD_FIXTURES[case]
    plugin = builder(tmp_path)

    result = await translate_tool.execute({"source_dir": str(plugin)})

    assert result.success is False
    assert result.error_code == expected_code
    assert result.error  # 明确报错文本（错误是值，可恢复）


# ── 服务端接线（server.py 注册面 → 工具实现 → 失败信封）──────────────────


async def test_server_translate_handler_returns_output_and_failure_envelope(
    mit_skills_plugin: Path, tmp_path: Path
) -> None:
    server = load_server_module()

    ok = await server.ecosystem_translate(source_dir=str(mit_skills_plugin))
    assert ok["plugin"]["name"] == "superpowers-lite"
    assert ok["skills"]

    failed = await server.ecosystem_translate(source_dir=str(tmp_path / "nowhere"))
    assert failed["success"] is False
    assert failed["error"]
