# @feature: FP-0.2.外部生态导入 B批 | @ci: python-coverage
"""``ecosystem_apply``（Claude Code 落地 + 未声明许可证开关）行为测试。

验收面：形态 2 落地技能集合 == 声明集合（未声明路径零落地）、形态 1 只落技能
（agents/commands/hooks 不落地）、形态 3 裸根落地、未声明许可证开关三态
（默认拒 / 开启放行并留痕 / 已知排除许可证开启仍拒）、非布尔开关 fail-closed。
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
)
from codex_fixtures import skill_md
from license_gate import check_license

pytestmark = pytest.mark.unit


def _provenance(user_root: Path) -> dict:
    return json.loads((user_root / "ecosystem_imports.json").read_text(encoding="utf-8"))


@pytest.fixture
def landing_marketplace(tmp_path: Path) -> Path:
    """形态 2：一个条目逐项声明技能目录，另有一个仓库内未声明的技能目录。"""
    root = tmp_path / "landing-repo"
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
            }
        ],
    )
    add_skill_dir(root / "skills", "xlsx", content=skill_md("xlsx", "电子表格"))
    add_skill_dir(root / "skills", "pdf", content=skill_md("pdf", "PDF 处理"))
    add_skill_dir(root / "template", "skeleton", content=skill_md("skeleton", "模板"))
    return root


@pytest.fixture
def mixed_claude_plugin(tmp_path: Path) -> Path:
    """形态 1：2 技能 + agents + commands + hooks（MIT）。"""
    plugin = make_claude_plugin(tmp_path, name="ui-kit", version="2.1.0", license="MIT", description="界面工具箱")
    add_skill_dir(plugin / "skills", "review", content=skill_md("review", "审查界面代码"))
    add_skill_dir(plugin / "skills", "scaffold", content=skill_md("scaffold", "脚手架生成"))
    write_agents(plugin, {"reviewer.md": "---\nname: reviewer\nmodel: sonnet\n---\n\n审查。\n"})
    write_commands(plugin, {"review.md": "---\ndescription: 审查\n---\n\n审查。\n"})
    write_hooks(
        plugin, {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "python3 g.py"}]}]}
    )
    return plugin


async def test_apply_marketplace_entry_lands_declared_skills_only(
    landing_marketplace: Path, user_root_env: Path, apply_tool
) -> None:
    result = await apply_tool.execute(
        {"source_dir": str(landing_marketplace), "kind": "claude_code", "entry": "document-skills"}
    )

    assert result.success is True
    landed = {s["skill_id"] for s in result.output["skills_written"]}
    # 性质：落地技能集合 == skills[] 声明集合（未声明的 template/skeleton 零落地）
    assert landed == {"xlsx", "pdf"}
    assert not (user_root_env / "skills" / "skeleton").exists()
    assert sorted(p.name for p in (user_root_env / "skills").iterdir() if p.is_dir()) == ["pdf", "xlsx"]
    record = _provenance(user_root_env)["imports"][0]
    assert record["translator"] == {"kind": "claude_code", "version": "1.0.0"}
    assert record["skills"] == ["xlsx", "pdf"]
    assert record["license"] == "MIT"
    assert record["license_override"] is False


async def test_apply_plugin_lands_skills_only_not_agents_commands_hooks(
    mixed_claude_plugin: Path, user_root_env: Path, apply_tool
) -> None:
    result = await apply_tool.execute({"source_dir": str(mixed_claude_plugin), "kind": "claude_code"})

    assert result.success is True
    assert [s["skill_id"] for s in result.output["skills_written"]] == ["review", "scaffold"]
    # 性质：落地面 = 技能集合（agents/commands/hooks 只上报，一个文件都不落地）
    assert sorted(p.name for p in (user_root_env / "skills").iterdir()) == ["review", "scaffold"]


async def test_apply_bare_root_lands_skills_with_license_override(
    tmp_path: Path, user_root_env: Path, apply_tool
) -> None:
    """形态 3 无清单（许可证必然未声明）：显式开关放行后落地并留痕。"""
    root = tmp_path / "claude-dir" / "skills"  # 真实形态：技能根名为 skills（~/.claude/skills）
    add_skill_dir(root, "git-workflow", content=skill_md("git-workflow", "Git 工作流"))
    add_skill_dir(root, "hard-constraints", content=skill_md("hard-constraints", "硬约束"))

    result = await apply_tool.execute(
        {"source_dir": str(root), "kind": "claude_code", "allow_undeclared_license": True}
    )

    assert result.success is True
    assert sorted(s["skill_id"] for s in result.output["skills_written"]) == ["git-workflow", "hard-constraints"]
    assert result.output["license_override"] is True
    record = _provenance(user_root_env)["imports"][0]
    assert record["license"] is None
    assert record["license_override"] is True


def _undeclared_license_plugin(tmp_path: Path) -> Path:
    plugin = make_claude_plugin(tmp_path, name="undeclared-license", license=None)
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))
    (plugin / "LICENSE.md").write_text("# 许可\n", encoding="utf-8")
    return plugin


async def test_apply_rejects_undeclared_license_by_default(tmp_path: Path, user_root_env: Path, apply_tool) -> None:
    result = await apply_tool.execute({"source_dir": str(_undeclared_license_plugin(tmp_path)), "kind": "claude_code"})

    assert result.success is False
    assert result.error_code == "LICENSE_REJECTED"
    # 拒收理由含「未声明」与 license_file 事实提示（人工可复核）
    assert "缺失" in result.error
    assert "LICENSE.md" in result.error
    assert not (user_root_env / "skills").exists()
    assert not (user_root_env / "ecosystem_imports.json").exists()


async def test_apply_switch_lands_undeclared_license_with_override_trace(
    tmp_path: Path, user_root_env: Path, apply_tool
) -> None:
    result = await apply_tool.execute(
        {
            "source_dir": str(_undeclared_license_plugin(tmp_path)),
            "kind": "claude_code",
            "allow_undeclared_license": True,
        }
    )

    assert result.success is True
    assert result.output["license_override"] is True
    assert (user_root_env / "skills" / "alpha" / "SKILL.md").is_file()
    record = _provenance(user_root_env)["imports"][0]
    assert record["license"] is None
    assert record["license_override"] is True


async def test_apply_switch_does_not_override_known_excluded_license(
    tmp_path: Path, user_root_env: Path, apply_tool
) -> None:
    """已知且被清单排除的许可证（Proprietary）即使开关打开也照旧拒。"""
    plugin = make_claude_plugin(tmp_path, name="proprietary-plugin", license="Proprietary")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))

    result = await apply_tool.execute(
        {"source_dir": str(plugin), "kind": "claude_code", "allow_undeclared_license": True}
    )

    assert result.success is False
    assert result.error_code == "LICENSE_REJECTED"
    assert "Proprietary" in result.error
    assert not (user_root_env / "skills").exists()


async def test_apply_switch_unused_when_license_declared(
    mixed_claude_plugin: Path, user_root_env: Path, apply_tool
) -> None:
    """清单已声明 MIT：开关传 True 也不改变判定，留痕记 False（只标记实际豁免）。"""
    result = await apply_tool.execute(
        {"source_dir": str(mixed_claude_plugin), "kind": "claude_code", "allow_undeclared_license": True}
    )

    assert result.success is True
    assert result.output["license_override"] is False
    assert _provenance(user_root_env)["imports"][0]["license_override"] is False


@pytest.mark.parametrize("value", ["true", "false", 1, None])
async def test_apply_rejects_non_bool_license_switch(
    tmp_path: Path, user_root_env: Path, apply_tool, value: object
) -> None:
    """非布尔开关值 fail-closed（不静默做真值转换——"false" 的布尔转换是 True）。"""
    plugin = make_claude_plugin(tmp_path, name="bool-guard", license=None)
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))

    result = await apply_tool.execute(
        {"source_dir": str(plugin), "kind": "claude_code", "allow_undeclared_license": value}
    )

    assert result.success is False
    assert result.error_code == "OPTION_INVALID"
    assert not (user_root_env / "skills").exists()


async def test_apply_rejects_unsupported_kind(tmp_path: Path, user_root_env: Path, apply_tool) -> None:
    plugin = make_claude_plugin(tmp_path, name="kind-guard", license="MIT")
    add_skill_dir(plugin / "skills", "alpha", content=skill_md("alpha", "甲"))

    result = await apply_tool.execute({"source_dir": str(plugin), "kind": "claude-code"})

    assert result.success is False
    assert result.error_code == "KIND_UNSUPPORTED"
    assert not (user_root_env / "skills").exists()


@pytest.mark.parametrize(
    ("value", "allowed", "expected_license"),
    [
        (None, True, None),
        ("", True, None),
        ("Proprietary", False, "Proprietary"),
        ("MIT", True, "MIT"),
    ],
)
def test_check_license_switch_only_covers_absence(
    value: str | None, allowed: bool, expected_license: str | None
) -> None:
    gate = check_license(value, allow_undeclared=True)

    assert gate.allowed is allowed
    assert gate.license == expected_license


def test_check_license_reason_carries_license_file_fact() -> None:
    with_hint = check_license(None, license_file="LICENSE.md")
    without_hint = check_license(None)

    assert with_hint.allowed is False
    assert "缺失" in with_hint.reason
    assert "LICENSE.md" in with_hint.reason
    # 性质：无 license_file 时理由如实说明未发现（不留悬念），"缺失"语义两种均可辨别
    assert "缺失" in without_hint.reason
    assert "LICENSE.md" not in without_hint.reason


async def test_server_apply_handler_serves_claude_kind(
    landing_marketplace: Path, user_root_env: Path, tmp_path: Path
) -> None:
    from codex_fixtures import load_server_module

    server = load_server_module()

    ok = await server.ecosystem_apply(
        source_dir=str(landing_marketplace), kind="claude_code", entry="document-skills", force=False
    )
    assert sorted(s["skill_id"] for s in ok["skills_written"]) == ["pdf", "xlsx"]
    assert ok["license_override"] is False

    failed = await server.ecosystem_apply(source_dir=str(tmp_path / "nowhere"), kind="claude_code", force=False)
    assert failed["success"] is False
    assert failed["error"]
