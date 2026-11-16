# @feature: FP-0.2.外部生态导入 B批 | @ci: python-coverage
"""``ecosystem_translate``（Claude Code 翻译器）行为测试。

验收面：三形态（插件目录 / 市场条目 / 裸技能目录）的身份与技能清单、
agents/commands/hooks 事实摘要（不得变成可落地技能）、license_file 事实位、
MCP 事实位（两种规范形态 + OAuth 块 + 凭据零泄漏）、旧形态与坏件 fail-closed、
只读零副作用。

≥2 组有区分度夹具：形态 1 混合包（技能 + agents + commands + hooks）vs 形态 2
两条目（逐项技能目录 / 容器目录）vs 形态 3 裸根（混入无 SKILL.md 条目）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_fixtures import (
    add_skill_dir,
    make_claude_plugin,
    make_marketplace,
    write_agents,
    write_commands,
    write_hooks,
    write_mcp,
)
from codex_fixtures import file_snapshot, load_server_module, skill_md

pytestmark = pytest.mark.unit


# ── 形态 1：插件目录 ─────────────────────────────────────────────────────


@pytest.fixture
def claude_plugin(tmp_path: Path) -> Path:
    """形态 1 混合包：2 技能 + 子目录 agent + 子目录 command + hooks（无 license）。"""
    plugin = make_claude_plugin(
        tmp_path, name="ui-kit", version="2.1.0", license=None, description="界面工具箱", author="Demo Author"
    )
    add_skill_dir(
        plugin / "skills",
        "review",
        content=skill_md("review", "审查界面代码", body="先看 references/checklist.md。\n"),
        extra_files={"references/checklist.md": "# 清单\n"},
    )
    add_skill_dir(plugin / "skills", "scaffold", content=skill_md("scaffold", "脚手架生成"))
    # 非技能条目（散文件 / 无 SKILL.md 的目录）不构成技能，也不算坏件
    (plugin / "skills" / "notes.txt").write_text("散文件\n", encoding="utf-8")
    (plugin / "skills" / "empty-dir").mkdir()
    write_agents(
        plugin,
        {
            "reviewer.md": "---\nname: reviewer\ndescription: 审查\ntools: Read, Grep, Bash\nmodel: sonnet\n---\n\n审查代码。\n",
            "nested/architect.md": "---\nname: architect\nmodel: opus\n---\n\n设计。\n",
        },
    )
    write_commands(
        plugin,
        {
            "review.md": "---\ndescription: 审查\n---\n\n审查。\n",
            "db/migrate.md": "---\ndescription: 迁移\n---\n\n迁移。\n",
        },
    )
    write_hooks(
        plugin,
        {
            "PreToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "python3 guard.py"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "python3 boot.py"}]}],
        },
    )
    return plugin


async def test_translate_plugin_dir_reports_identity_skills_and_layout_facts(
    claude_plugin: Path, translate_tool
) -> None:
    result = await translate_tool.execute({"source_dir": str(claude_plugin), "kind": "claude_code"})

    assert result.success is True
    output = result.output
    assert output["kind"] == "claude_code"
    assert output["plugin"]["name"] == "ui-kit"
    assert output["plugin"]["version"] == "2.1.0"
    assert output["plugin"]["author"] == "Demo Author"
    assert output["plugin"]["license"] is None
    assert output["license_file"] is None  # 源目录与仓库根均无 LICENSE
    assert [s["skill_id"] for s in output["skills"]] == ["review", "scaffold"]
    assert output["agents"] == [
        {"name": "architect", "model": "opus", "tools_count": 0},
        {"name": "reviewer", "model": "sonnet", "tools_count": 3},
    ]
    assert [c["name"] for c in output["commands"]] == ["db/migrate", "review"]
    assert output["hooks"] == {"declared": True, "events": ["PreToolUse", "SessionStart"]}
    assert output["mcp_servers"] == []
    assert output["apps"] == []
    # 性质：技能清单 == 磁盘上带 SKILL.md 的技能目录全集；agents/commands/hooks 不入技能面
    on_disk = sorted(p.name for p in (claude_plugin / "skills").iterdir() if (p / "SKILL.md").is_file())
    assert sorted(s["skill_id"] for s in output["skills"]) == on_disk == ["review", "scaffold"]


async def test_translate_plugin_dir_reports_license_file_hint(tmp_path: Path, translate_tool) -> None:
    """未声明许可证时如实报告源目录找到的 LICENSE 文件名（事实位，不自动采用）。"""
    plugin = make_claude_plugin(tmp_path, name="licensed-dir", license=None)
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    (plugin / "LICENSE.md").write_text("# 许可\n", encoding="utf-8")

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    assert result.output["license_file"] == "LICENSE.md"


async def test_translate_plugin_dir_license_file_comes_from_repo_root(tmp_path: Path, translate_tool) -> None:
    """源目录本身无 LICENSE 时，取「所属仓库根」（最近带 .git 的祖先）的 LICENSE 文件名。"""
    repo = tmp_path / "marketplace-repo"
    plugin = make_claude_plugin(repo / "plugins", name="nested-plugin", license=None)
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    (repo / ".git").mkdir()
    (repo / "LICENSE").write_text("仓库根许可\n", encoding="utf-8")

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    assert result.output["license_file"] == "LICENSE"


async def test_translate_plugin_dir_prefers_own_license_over_repo_root(tmp_path: Path, translate_tool) -> None:
    repo = tmp_path / "another-repo"
    plugin = make_claude_plugin(repo / "plugins", name="own-license-plugin", license="MIT")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    (repo / ".git").mkdir()
    (repo / "LICENSE").write_text("仓库根许可\n", encoding="utf-8")
    (plugin / "LICENSE.txt").write_text("插件自带许可\n", encoding="utf-8")

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    assert result.output["license_file"] == "LICENSE.txt"
    assert result.output["license_gate"]["allowed"] is True


async def test_translate_plugin_dir_reports_inline_manifest_hooks(tmp_path: Path, translate_tool) -> None:
    """清单内联 hooks（事件映射本体，无包裹层）计入声明事实。"""
    plugin = make_claude_plugin(tmp_path, name="inline-hooks", license="MIT", manifest_extra={"hooks": {"Stop": []}})
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    assert result.output["hooks"] == {"declared": True, "events": ["Stop"]}


async def test_translate_plugin_dir_hooks_path_form_declares_without_reading(tmp_path: Path, translate_tool) -> None:
    """清单 hooks 以文件路径转指：只记「已声明」，事件清单不跟读（钩子面本批不导入）。"""
    plugin = make_claude_plugin(
        tmp_path, name="path-hooks", license="MIT", manifest_extra={"hooks": "./hooks/extra.json"}
    )
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    assert result.output["hooks"] == {"declared": True, "events": []}


async def test_translate_plugin_dir_without_layout_faces_reports_empty_facts(tmp_path: Path, translate_tool) -> None:
    plugin = make_claude_plugin(tmp_path, name="bare-bones", license="MIT")

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    assert result.output["skills"] == []
    assert result.output["agents"] == []
    assert result.output["commands"] == []
    assert result.output["hooks"] == {"declared": False, "events": []}


@pytest.mark.parametrize("wrapped", [True, False])
async def test_translate_plugin_dir_mcp_facts_without_credentials(
    tmp_path: Path, translate_tool, wrapped: bool
) -> None:
    """``.mcp.json`` 两种规范形态同语义；OAuth 只报事实位，凭据/端点零泄漏。"""
    plugin = make_claude_plugin(tmp_path, name="mcp-plugin", license="MIT")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    write_mcp(
        plugin,
        {
            "github": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "oauth": {"clientId": "cid-9f2e", "callbackPort": 8080},
                "headers": {"Authorization": "Bearer ghp_secret_value"},
            },
            "local-tools": {"command": "cmd.exe", "args": ["/c", "run.cmd"]},
        },
        wrapped=wrapped,
    )

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    servers = {s["name"]: s for s in result.output["mcp_servers"]}
    assert servers["github"] == {"name": "github", "transport": "http", "requires_oauth": True}
    assert servers["local-tools"] == {"name": "local-tools", "transport": "stdio", "requires_oauth": False}
    dumped = json.dumps(result.output, ensure_ascii=False)
    for secret in ("ghp_secret_value", "cid-9f2e", "callbackPort", "api.githubcopilot.com", "run.cmd"):
        assert secret not in dumped


async def test_translate_plugin_dir_rejects_manifest_skills_key(tmp_path: Path, translate_tool) -> None:
    """清单 ``skills`` 键（追加扫描目录语义）在本批范围外 → fail-closed 明示，不静默漏技能。"""
    plugin = make_claude_plugin(tmp_path, name="skills-key", license="MIT", manifest_extra={"skills": ["./extra"]})

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == "SKILLS_KEY_UNSUPPORTED"
    assert "skills" in result.error


# ── 形态 2：市场条目 ─────────────────────────────────────────────────────


@pytest.fixture
def claude_marketplace(tmp_path: Path) -> Path:
    """形态 2 市场：条目①逐项技能目录 + 条目②容器目录 + 条目③子目录默认扫描。

    未声明的 ``template/skeleton``（仓库内有 SKILL.md 但两个条目都未列）不得进入任一翻译结果。
    """
    root = tmp_path / "skills-repo"
    make_marketplace(
        root,
        entries=[
            {
                "name": "document-skills",
                "description": "文档套件",
                "source": "./",
                "strict": False,
                "license": "MIT",
                "skills": ["./skills/xlsx", "./skills/pdf"],
            },
            {
                "name": "example-skills",
                "description": "示例集",
                "source": "./",
                "strict": False,
                "skills": ["./extra"],
            },
            {"name": "sub-pkg", "description": "子目录包", "source": "./sub-pkg"},
        ],
    )
    add_skill_dir(root / "skills", "xlsx", content=skill_md("xlsx", "电子表格"))
    add_skill_dir(root / "skills", "pdf", content=skill_md("pdf", "PDF 处理"))
    add_skill_dir(root / "extra", "art", content=skill_md("art", "算法艺术"))
    add_skill_dir(root / "extra", "brand", content=skill_md("brand", "品牌规范"))
    add_skill_dir(root / "template", "skeleton", content=skill_md("skeleton", "模板"))
    add_skill_dir(root / "sub-pkg" / "skills", "plain", content=skill_md("plain", "默认位技能"))
    return root


@pytest.mark.parametrize(
    ("entry", "expected_skills"),
    [("document-skills", ["pdf", "xlsx"]), ("example-skills", ["art", "brand"]), ("sub-pkg", ["plain"])],
)
async def test_translate_marketplace_follows_declared_skill_paths(
    claude_marketplace: Path, translate_tool, entry: str, expected_skills: list[str]
) -> None:
    result = await translate_tool.execute(
        {"source_dir": str(claude_marketplace), "kind": "claude_code", "entry": entry}
    )

    assert result.success is True
    output = result.output
    assert output["plugin"]["name"] == entry
    assert sorted(s["skill_id"] for s in output["skills"]) == expected_skills
    # 性质：未声明路径（template/skeleton）不进入任何条目的技能清单
    assert "skeleton" not in {s["skill_id"] for s in output["skills"]}


async def test_translate_marketplace_carries_entry_display_fields(claude_marketplace: Path, translate_tool) -> None:
    result = await translate_tool.execute(
        {"source_dir": str(claude_marketplace), "kind": "claude_code", "entry": "document-skills"}
    )

    assert result.success is True
    assert result.output["plugin"]["description"] == "文档套件"
    assert result.output["plugin"]["license"] == "MIT"
    assert result.output["license_gate"]["allowed"] is True


async def test_translate_marketplace_defaults_to_first_entry(claude_marketplace: Path, translate_tool) -> None:
    result = await translate_tool.execute({"source_dir": str(claude_marketplace), "kind": "claude_code"})

    assert result.success is True
    assert result.output["plugin"]["name"] == "document-skills"


async def test_translate_marketplace_entry_not_found_lists_available(claude_marketplace: Path, translate_tool) -> None:
    result = await translate_tool.execute(
        {"source_dir": str(claude_marketplace), "kind": "claude_code", "entry": "nope"}
    )

    assert result.success is False
    assert result.error_code == "ENTRY_NOT_FOUND"
    assert "example-skills" in result.error


def _single_entry_marketplace(tmp_path: Path, entry: dict) -> Path:
    root = tmp_path / "one-entry-repo"
    make_marketplace(root, entries=[entry])
    return root


def _marketplace_skills_missing(tmp_path: Path) -> Path:
    root = _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./", "skills": ["./skills/ghost"]})
    (root / "skills").mkdir(parents=True)
    return root


def _marketplace_skills_is_file(tmp_path: Path) -> Path:
    root = _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./", "skills": ["./skills/xlsx/SKILL.md"]})
    add_skill_dir(root / "skills", "xlsx", content=skill_md("xlsx", "电子表格"))
    return root


def _marketplace_skills_traversal(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./", "skills": ["./skills/../outside"]})


def _marketplace_skills_not_string(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./", "skills": [123]})


def _marketplace_entries_no_objects(tmp_path: Path) -> Path:
    root = tmp_path / "entries-no-objects-repo"
    make_marketplace(root, entries=[])
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "demo-marketplace", "owner": {"name": "Demo Owner"}, "plugins": [1, "x"]}),
        encoding="utf-8",
    )
    return root


def _marketplace_skills_without_dot_prefix(tmp_path: Path) -> Path:
    root = _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./", "skills": ["skills/xlsx"]})
    add_skill_dir(root / "skills", "xlsx", content=skill_md("xlsx", "电子表格"))
    return root


def _marketplace_skills_empty_dir(tmp_path: Path) -> Path:
    root = _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./", "skills": ["./empty"]})
    (root / "empty").mkdir(parents=True)
    return root


def _marketplace_source_remote(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg", "source": {"source": "github", "repo": "x/y"}})


def _marketplace_source_traversal(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./../outside"})


def _marketplace_source_bare_name(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "plugins/pkg"})


def _marketplace_source_missing_dir(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./nowhere"})


def _marketplace_source_self_manifested(tmp_path: Path) -> Path:
    root = _single_entry_marketplace(tmp_path, {"name": "pkg", "source": "./plugins/pkg"})
    plugin = make_claude_plugin(root / "plugins", name="pkg", license="MIT")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    return root


def _marketplace_no_plugins(tmp_path: Path) -> Path:
    root = tmp_path / "plugins-missing-repo"
    make_marketplace(root, entries=[])
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "demo-marketplace", "owner": {"name": "Demo Owner"}}), encoding="utf-8"
    )
    return root


def _marketplace_broken_json(tmp_path: Path) -> Path:
    root = tmp_path / "broken-marketplace-repo"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text("{ 半截 JSON", encoding="utf-8")
    return root


def _marketplace_entry_without_name(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"source": "./"})


def _marketplace_entry_without_source(tmp_path: Path) -> Path:
    return _single_entry_marketplace(tmp_path, {"name": "pkg"})


_MARKETPLACE_BAD_FIXTURES = {
    "skills-path-missing": (_marketplace_skills_missing, "SKILL_PATH_NOT_FOUND"),
    "skills-path-is-file": (_marketplace_skills_is_file, "SKILL_PATH_INVALID"),
    "skills-path-traversal": (_marketplace_skills_traversal, "SKILL_PATH_INVALID"),
    "skills-path-not-string": (_marketplace_skills_not_string, "SKILL_PATH_INVALID"),
    "skills-path-without-dot-prefix": (_marketplace_skills_without_dot_prefix, "SKILL_PATH_INVALID"),
    "skills-path-empty-dir": (_marketplace_skills_empty_dir, "SKILL_PATH_EMPTY"),
    "source-remote": (_marketplace_source_remote, "SOURCE_NOT_LOCAL"),
    "source-traversal": (_marketplace_source_traversal, "SOURCE_INVALID"),
    "source-bare-name": (_marketplace_source_bare_name, "SOURCE_INVALID"),
    "source-missing-dir": (_marketplace_source_missing_dir, "SOURCE_NOT_FOUND"),
    "source-self-manifested": (_marketplace_source_self_manifested, "ENTRY_SELF_MANIFESTED"),
    "no-plugins-array": (_marketplace_no_plugins, "MARKETPLACE_INVALID"),
    "no-entry-objects": (_marketplace_entries_no_objects, "MARKETPLACE_INVALID"),
    "broken-json": (_marketplace_broken_json, "MARKETPLACE_INVALID"),
    "entry-without-name": (_marketplace_entry_without_name, "MARKETPLACE_INVALID"),
    "entry-without-source": (_marketplace_entry_without_source, "MARKETPLACE_INVALID"),
}


@pytest.mark.parametrize("case", sorted(_MARKETPLACE_BAD_FIXTURES))
async def test_translate_marketplace_bad_fixture_fails_closed(tmp_path: Path, translate_tool, case: str) -> None:
    builder, expected_code = _MARKETPLACE_BAD_FIXTURES[case]
    source = builder(tmp_path)

    result = await translate_tool.execute({"source_dir": str(source), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == expected_code
    assert result.error  # 明确报错文本（错误是值，可恢复）


# ── 形态 3：裸技能目录 ───────────────────────────────────────────────────


@pytest.fixture
def claude_bare_root(tmp_path: Path) -> Path:
    """形态 3：裸根两技能 + 混入散文件与无 SKILL.md 的子目录（跳过不崩）。"""
    root = tmp_path / "bare-skills"
    add_skill_dir(root, "git-workflow", content=skill_md("git-workflow", "Git 工作流"))
    add_skill_dir(root, "hard-constraints", content=skill_md("hard-constraints", "硬约束"))
    (root / "README.md").write_text("# 说明\n", encoding="utf-8")
    (root / "not-a-skill").mkdir()
    (root / "not-a-skill" / "notes.md").write_text("无 SKILL.md\n", encoding="utf-8")
    return root


async def test_translate_bare_root_reports_skills_without_manifest(claude_bare_root: Path, translate_tool) -> None:
    result = await translate_tool.execute({"source_dir": str(claude_bare_root), "kind": "claude_code"})

    assert result.success is True
    output = result.output
    assert output["plugin"] == {"name": "bare-skills", "version": "", "description": "", "author": "", "license": None}
    assert [s["skill_id"] for s in output["skills"]] == ["git-workflow", "hard-constraints"]
    assert output["hooks"] == {"declared": False, "events": []}
    # 性质：技能集合 == 一级子目录中带 SKILL.md 的全集（散条目不算技能）
    on_disk = sorted(p.name for p in claude_bare_root.iterdir() if (p / "SKILL.md").is_file())
    assert sorted(s["skill_id"] for s in output["skills"]) == on_disk


async def test_translate_bare_root_reports_sibling_license_file(tmp_path: Path, translate_tool) -> None:
    root = tmp_path / "bare-with-license"
    add_skill_dir(root, "alpha", content=skill_md("alpha", "甲"))
    (root / "LICENSE").write_text("许可\n", encoding="utf-8")

    result = await translate_tool.execute({"source_dir": str(root), "kind": "claude_code"})

    assert result.success is True
    assert result.output["license_file"] == "LICENSE"


async def test_translate_plugin_dir_agent_facts_survive_broken_frontmatter(tmp_path: Path, translate_tool) -> None:
    """agents 是只上报事实面：frontmatter 坏/缺时字段整体忽略（装载语义），不拖垮整包翻译。"""
    plugin = make_claude_plugin(tmp_path, name="agent-facts", license="MIT")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    write_agents(
        plugin,
        {
            "list-tools.md": "---\nname: list-tools\ntools:\n  - Read\n  - Grep\n---\n\n用列表形态声明工具。\n",
            "broken-yaml.md": "---\nname: [未闭合\n---\n\n坏 frontmatter。\n",
            "no-frontmatter.md": "纯正文，无 frontmatter。\n",
        },
    )
    (plugin / "agents" / "not-utf8.md").write_bytes(b"\xff\xfe\x00not utf8")
    (plugin / "agents" / "empty-tools.md").write_text(
        '---\nname: empty-tools\ntools: ""\n---\n\n空工具。\n', encoding="utf-8"
    )

    result = await translate_tool.execute({"source_dir": str(plugin), "kind": "claude_code"})

    assert result.success is True
    facts = {item["name"]: item for item in result.output["agents"]}
    assert facts["list-tools"] == {"name": "list-tools", "model": "", "tools_count": 2}
    # 性质：坏件与非法条目都只损失自身字段（名回落文件名），不改变其余 agents 事实
    assert facts["broken-yaml"]["tools_count"] == 0
    assert facts["no-frontmatter"]["name"] == "no-frontmatter"
    assert facts["not-utf8"] == {"name": "not-utf8", "model": "", "tools_count": 0}
    assert facts["empty-tools"]["tools_count"] == 0


# ── 形态判定与坏件 ───────────────────────────────────────────────────────


def _legacy_root_marketplace(tmp_path: Path) -> Path:
    root = tmp_path / "legacy-marketplace"
    root.mkdir()
    (root / "marketplace.json").write_text(json.dumps({"name": "legacy"}), encoding="utf-8")
    return root


def _legacy_root_plugin(tmp_path: Path) -> Path:
    root = tmp_path / "legacy-plugin"
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps({"name": "legacy"}), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("builder", "mentioned"),
    [(_legacy_root_marketplace, "marketplace.json"), (_legacy_root_plugin, "plugin.json")],
)
async def test_translate_legacy_root_manifest_fails_closed(tmp_path: Path, translate_tool, builder, mentioned) -> None:
    """旧形态（根级清单，无 .claude-plugin/ 前缀）如实报识别物并拒收，无兼容分支。"""
    result = await translate_tool.execute({"source_dir": str(builder(tmp_path)), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == "LEGACY_LAYOUT"
    assert mentioned in result.error
    assert "旧形态" in result.error


async def test_translate_unrecognized_layout_fails_closed(tmp_path: Path, translate_tool) -> None:
    empty = tmp_path / "not-claude"
    empty.mkdir()

    result = await translate_tool.execute({"source_dir": str(empty), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == "LAYOUT_UNRECOGNIZED"


def _plugin_manifest_without_name(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-noname")
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    return plugin


def _plugin_manifest_broken_json(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-json")
    (plugin / ".claude-plugin" / "plugin.json").write_text("{ 不是 JSON", encoding="utf-8")
    return plugin


def _plugin_manifest_not_object(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-notobject")
    (plugin / ".claude-plugin" / "plugin.json").write_text("[1, 2, 3]", encoding="utf-8")
    return plugin


def _broken_skill_frontmatter(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-skill-yaml", license="MIT")
    add_skill_dir(plugin / "skills", "bad", content="---\nname: [未闭合\n---\n\n正文\n")
    return plugin


def _skill_frontmatter_not_mapping(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-skill-nonmap", license="MIT")
    add_skill_dir(plugin / "skills", "bad", content="---\n- 甲\n- 乙\n---\n\n正文\n")
    return plugin


def _skill_not_utf8(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-skill-encoding", license="MIT")
    skill_dir = plugin / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe\x00---\nname: bad\n")
    return plugin


def _mcp_without_endpoint(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-mcp-noendpoint", license="MIT")
    write_mcp(plugin, {"ghost": {"type": "http"}})
    return plugin


def _mcp_server_not_object(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-mcp-nonobject", license="MIT")
    write_mcp(plugin, {"ghost": "https://example.invalid/mcp"}, wrapped=False)
    return plugin


def _mcp_broken_json(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-mcp-json", license="MIT")
    (plugin / ".mcp.json").write_text("[1, 2", encoding="utf-8")
    return plugin


def _mcp_servers_not_map(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-mcp-nomap", license="MIT")
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": ["not-a-map"]}), encoding="utf-8")
    return plugin


def _hooks_broken_json(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-hooks-json", license="MIT")
    path = plugin / "hooks" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ 半截", encoding="utf-8")
    return plugin


def _hooks_without_wrapper(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="broken-hooks-nowrapper", license="MIT")
    path = plugin / "hooks" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"PreToolUse": []}), encoding="utf-8")
    return plugin


_BAD_FIXTURES = {
    "manifest-without-name": (_plugin_manifest_without_name, "MANIFEST_INVALID"),
    "manifest-broken-json": (_plugin_manifest_broken_json, "MANIFEST_INVALID"),
    "manifest-not-object": (_plugin_manifest_not_object, "MANIFEST_INVALID"),
    "broken-skill-frontmatter": (_broken_skill_frontmatter, "SKILL_INVALID"),
    "skill-frontmatter-not-mapping": (_skill_frontmatter_not_mapping, "SKILL_INVALID"),
    "skill-not-utf8": (_skill_not_utf8, "SKILL_INVALID"),
    "mcp-without-endpoint": (_mcp_without_endpoint, "MCP_INVALID"),
    "mcp-server-not-object": (_mcp_server_not_object, "MCP_INVALID"),
    "mcp-broken-json": (_mcp_broken_json, "MCP_INVALID"),
    "mcp-servers-not-map": (_mcp_servers_not_map, "MCP_INVALID"),
    "hooks-broken-json": (_hooks_broken_json, "HOOKS_INVALID"),
    "hooks-without-wrapper": (_hooks_without_wrapper, "HOOKS_INVALID"),
}


@pytest.mark.parametrize("case", sorted(_BAD_FIXTURES))
async def test_translate_bad_fixture_fails_closed(tmp_path: Path, translate_tool, case: str) -> None:
    builder, expected_code = _BAD_FIXTURES[case]
    source = builder(tmp_path)

    result = await translate_tool.execute({"source_dir": str(source), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == expected_code
    assert result.error


@pytest.mark.parametrize("args", [{}, {"source_dir": "   "}])
async def test_translate_rejects_missing_source_dir(translate_tool, args: dict[str, str]) -> None:
    result = await translate_tool.execute({**args, "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == "SOURCE_NOT_FOUND"


async def test_translate_rejects_missing_directory_claude_kind(tmp_path: Path, translate_tool) -> None:
    result = await translate_tool.execute({"source_dir": str(tmp_path / "nowhere"), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == "SOURCE_NOT_FOUND"


# ── 只读与工具接线 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fixture_name", "extra"),
    [("claude_plugin", {}), ("claude_marketplace", {"entry": "document-skills"}), ("claude_bare_root", {})],
)
async def test_translate_is_read_only(
    tmp_path: Path, request: pytest.FixtureRequest, translate_tool, fixture_name: str, extra: dict
) -> None:
    """三形态翻译全程只读：源目录快照前后逐字节一致（零写入铁律）。"""
    source = request.getfixturevalue(fixture_name)
    before = file_snapshot(tmp_path)

    result = await translate_tool.execute({"source_dir": str(source), "kind": "claude_code", **extra})

    assert result.success is True
    assert file_snapshot(tmp_path) == before


async def test_translate_rejects_unsupported_kind(claude_plugin: Path, translate_tool) -> None:
    result = await translate_tool.execute({"source_dir": str(claude_plugin), "kind": "claude-code"})

    assert result.success is False
    assert result.error_code == "KIND_UNSUPPORTED"


async def test_server_translate_handler_serves_claude_kind(claude_marketplace: Path, tmp_path: Path) -> None:
    server = load_server_module()

    ok = await server.ecosystem_translate(
        source_dir=str(claude_marketplace), kind="claude_code", entry="example-skills"
    )
    assert ok["plugin"]["name"] == "example-skills"
    assert [s["skill_id"] for s in ok["skills"]] == ["art", "brand"]

    failed = await server.ecosystem_translate(source_dir=str(tmp_path / "nowhere"), kind="claude_code")
    assert failed["success"] is False
    assert failed["error"]
