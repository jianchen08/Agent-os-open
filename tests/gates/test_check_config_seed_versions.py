# @feature: FP-0.2.CFG 出厂播种版本门禁 | @ci: python-test
"""check_config_seed_versions.py 单测（ADR 2026-10-24-config-seed-version-arbitration）。

行为契约（断输入→输出，不钉实现）：
- 顶层 version 提取与 electron seedConfigTree.topLevelVersion 同口径：列首
  点分整数命中（行尾注释容忍），缩进/列表项/非点分/缺席一律 None；
- 在场声明：受管区缺声明打红，有声明入索引；
- 变更必抬版本：head 与基线版本相同（含双方 None→None 之外的持平）打红，
  抬过/新增文件（基线 None）放行，已删除与无声明文件不在此查；
- 负控制：真实仓状态全绿（现役管道 yaml 均带 version，最近提交无持平变更）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_config_seed_versions", SCRIPTS / "check_config_seed_versions.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ── top_level_version：与 electron 播种侧同口径 ─────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("version: 1\nname: p\n", "1"),
        ("name: p\nversion: 2.10.3\n", "2.10.3"),
        ("version: 1  # 出厂播种版本\n", "1"),  # 行尾注释容忍
        ("version: 01\n", "01"),  # 前导零仍是点分整数（数值比较在播种侧）
    ],
)
def test_top_level_version_hits(text: str, expected: str) -> None:
    assert mod.top_level_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "name: p\n",  # 缺席
        "metadata:\n  version: 99\n",  # 缩进嵌套不算顶层
        "- version: 3\n",  # 列表项不算顶层
        "version: v2\n",  # 非点分整数
        "version: 1..2\n",
        "version:\n",  # 空值
        "VERSION: 1\n",  # 大小写敏感
    ],
)
def test_top_level_version_misses(text: str) -> None:
    assert mod.top_level_version(text) is None


# ── markdown 资产版本：与 electron seedConfigTree.markdownSeedVersion 同口径 ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<!-- seed-version: 1 -->\n# 规则\n", "1"),
        ("<!-- seed-version: 2.10 -->\n正文\n", "2.10"),
        ("\n<!-- seed-version: 2.10 -->\n正文\n", "2.10"),
        ("\ufeff<!-- seed-version: 3 -->\n正文\n", "3"),
        ("<!-- seed-version: 01 -->\n正文\n", "01"),
        ("<!--seed-version:1-->\n正文\n", "1"),
    ],
)
def test_markdown_version_hits(text: str, expected: str) -> None:
    assert mod.markdown_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",  # 无内容
        "# 规则\n<!-- seed-version: 1 -->\n",  # 标记不在首个非空行
        "<!-- seed-version: v1 -->\n",  # 非点分整数
        "<!-- seed-version:  -->\n",  # 空值
    ],
)
def test_markdown_version_misses(text: str) -> None:
    assert mod.markdown_version(text) is None


def test_asset_version_dispatch(tmp_path: Path) -> None:
    yaml_path = tmp_path / "a.yaml"
    md_path = tmp_path / "a.md"
    txt_path = tmp_path / "a.txt"
    assert mod.asset_version(yaml_path, "version: 2\n") == "2"
    assert mod.asset_version(md_path, "<!-- seed-version: 3 -->\n") == "3"
    assert mod.asset_version(txt_path, "version: 2\n") is None


# ── 在场声明 ───────────────────────────────────────────────────────


def test_missing_declarations_real_repo_green() -> None:
    managed = mod.managed_files()
    assert managed, "受管区不应为空"
    errors, versions = mod.missing_declarations(managed)
    assert errors == [], f"真实仓受管区应全绿，实际: {errors}"
    assert set(versions) == {
        p.relative_to(REPO_ROOT).as_posix() for p in managed
    }


def test_managed_files_includes_factory_policy_assets() -> None:
    """安全/隔离/评估策略与工具声明属于出厂受管面（陈旧副本改变运行时行为）。"""
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in mod.managed_files()}
    assert {
        "config/plugins/security_check/security_rules.yaml",
        "config/plugins/security_check/settings.yaml",
        "config/plugins/isolation/isolation_policy.yaml",
        "config/plugins/evaluation/evaluation_metrics.yaml",
        "config/tools/bash.yaml",
        "config/tools/builtin_tools_config.yaml",
        "config/tools/project_create.yaml",
    } <= rels
    # 用户数据配置不得进受管面（避免安装覆盖用户设置）
    assert "config/plugins/isolation/isolation_config.yaml" not in rels
    assert "config/plugins/llm/llm.yaml" not in rels


class _FakeManaged:
    """鸭子类型受管文件（missing_declarations 只消费 read_text/relative_to），
    合成违规不落仓内 config/（污染受管区会撞并行门禁）。"""

    def __init__(self, text: str, rel: str = "config/pipelines/__synthetic__.yaml") -> None:
        self._text = text
        self._rel = rel

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def relative_to(self, root: Path) -> Path:
        return Path(self._rel)


def test_missing_declarations_synthetic_violation() -> None:
    errors, versions = mod.missing_declarations([_FakeManaged("name: no-version\n")])
    assert len(errors) == 1
    assert "__synthetic__.yaml" in errors[0]
    assert versions == {}


def test_missing_declarations_synthetic_md_violation() -> None:
    errors, versions = mod.missing_declarations(
        [_FakeManaged("# 规则\n", "config/rules/__synthetic__.md")]
    )
    assert len(errors) == 1
    assert "__synthetic__.md" in errors[0]
    assert "seed-version" in errors[0]
    assert versions == {}


def test_missing_declarations_md_marker_ok() -> None:
    errors, versions = mod.missing_declarations(
        [
            _FakeManaged(
                "<!-- seed-version: 1 -->\n# 规则\n",
                "config/rules/__synthetic__.md",
            )
        ]
    )
    assert errors == []
    assert versions == {"config/rules/__synthetic__.md": "1"}


# ── 变更必抬版本 ───────────────────────────────────────────────────


def test_bump_violations_same_version_flagged() -> None:
    vios = mod.bump_violations(
        changed=["config/pipelines/autonomous.yaml"],
        head_versions={"config/pipelines/autonomous.yaml": "1"},
        base_version_of=lambda _rel: "1",
        exists=lambda _rel: True,
    )
    assert len(vios) == 1
    assert "未抬版本" in vios[0]


def test_bump_violations_bumped_or_new_pass() -> None:
    head = {"config/pipelines/autonomous.yaml": "2", "config/pipelines/new.yaml": "1"}
    base = {"config/pipelines/autonomous.yaml": "1", "config/pipelines/new.yaml": None}
    vios = mod.bump_violations(
        changed=list(head),
        head_versions=head,
        base_version_of=lambda rel: base[rel],
        exists=lambda _rel: True,
    )
    assert vios == []


def test_bump_violations_deleted_skipped_undeclared_flagged() -> None:
    # gone.yaml 已删（exists=False）→ 跳过；nover.yaml 版本持平 → 报。
    vios = mod.bump_violations(
        changed=["config/pipelines/gone.yaml", "config/pipelines/nover.yaml"],
        head_versions={"config/pipelines/nover.yaml": "1"},
        base_version_of=lambda _rel: "1",
        exists=lambda rel: rel.endswith("nover.yaml"),
    )
    assert len(vios) == 1
    assert "nover" in vios[0]


# ── 模式种子包：内容变更必抬版本 ─────────────────────────────────────


def test_mode_seed_changed_packages_groups_and_excludes_noise() -> None:
    changed = [
        "plugins/shared/mode_coding/agents/programming_orchestrator_agent_v2.yaml",
        "plugins/shared/mode_coding/skills/code-implement/SKILL.md",
        "plugins/shared/mode_coding/tests/test_server.py",
        "plugins/shared/mode_coding/__pycache__/server.cpython-312.pyc",
        "plugins/shared/mode_coding/.venv/Lib/site-packages/x.py",
        "plugins/shared/mode_coding/.venv-tools/lib/y.py",
        "plugins/shared/bash_tool/tool.py",
        "plugins/shared/_libs/testing/tool_ids_contract.py",
    ]
    pkgs = mod.mode_seed_changed_packages(changed)
    assert list(pkgs) == ["plugins/shared/mode_coding"]
    assert pkgs["plugins/shared/mode_coding"] == [
        "plugins/shared/mode_coding/agents/programming_orchestrator_agent_v2.yaml",
        "plugins/shared/mode_coding/skills/code-implement/SKILL.md",
    ]


def test_mode_seed_bump_violations_same_version_flagged() -> None:
    vios = mod.mode_seed_bump_violations(
        ["plugins/shared/mode_coding/agents/a.yaml"],
        head_version_of=lambda _pkg: "1.3.7",
        base_version_of=lambda _pkg: "1.3.7",
    )
    assert len(vios) == 1
    assert "未抬版本" in vios[0]
    assert "mode_coding" in vios[0]


def test_mode_seed_bump_violations_bumped_or_new_package_pass() -> None:
    vios = mod.mode_seed_bump_violations(
        [
            "plugins/shared/mode_coding/agents/a.yaml",
            "plugins/shared/mode_new/agents/b.yaml",
        ],
        head_version_of=lambda pkg: "1.3.8" if pkg.endswith("coding") else "0.1.0",
        base_version_of=lambda pkg: "1.3.7" if pkg.endswith("coding") else None,
    )
    assert vios == []


def test_mode_seed_bump_violations_missing_version_flagged() -> None:
    vios = mod.mode_seed_bump_violations(
        ["plugins/shared/mode_godot/server.py"],
        head_version_of=lambda _pkg: None,
        base_version_of=lambda _pkg: None,
    )
    assert len(vios) == 1
    assert "缺 version" in vios[0]


# ── 负控制：真实仓端到端全绿 ────────────────────────────────────────


def test_real_repo_gate_green() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_config_seed_versions.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert proc.returncode == 0, f"真实仓应全绿: {proc.stderr}"
    assert "通过" in proc.stdout
