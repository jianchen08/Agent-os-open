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
    assert spec is not None and spec.loader is not None
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


# ── 在场声明 ───────────────────────────────────────────────────────


def test_missing_declarations_real_repo_green() -> None:
    managed = sorted((REPO_ROOT / "config" / "pipelines").glob("*.yaml"))
    errors, versions = mod.missing_declarations(managed)
    assert errors == [], f"真实仓受管区应全绿，实际: {errors}"
    assert set(versions) == {
        p.relative_to(REPO_ROOT).as_posix() for p in managed
    }


class _FakeManaged:
    """鸭子类型受管文件（missing_declarations 只消费 read_text/relative_to），
    合成违规不落仓内 config/（污染受管区会撞并行门禁）。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def relative_to(self, root: Path) -> Path:
        return Path("config/pipelines/__synthetic__.yaml")


def test_missing_declarations_synthetic_violation() -> None:
    errors, versions = mod.missing_declarations([_FakeManaged("name: no-version\n")])
    assert len(errors) == 1
    assert "__synthetic__.yaml" in errors[0]
    assert versions == {}


# ── 变更必抬版本 ───────────────────────────────────────────────────


def test_bump_violations_same_version_flagged() -> None:
    vios = mod.bump_violations(
        changed=["config/pipelines/autonomous.yaml"],
        head_versions={"config/pipelines/autonomous.yaml": "1"},
        base_version_of=lambda rel: "1",
        exists=lambda rel: True,
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
        exists=lambda rel: True,
    )
    assert vios == []


def test_bump_violations_deleted_skipped_undeclared_flagged() -> None:
    # gone.yaml 已删（exists=False）→ 跳过；nover.yaml 版本持平 → 报。
    vios = mod.bump_violations(
        changed=["config/pipelines/gone.yaml", "config/pipelines/nover.yaml"],
        head_versions={"config/pipelines/nover.yaml": "1"},
        base_version_of=lambda rel: "1",
        exists=lambda rel: rel.endswith("nover.yaml"),
    )
    assert len(vios) == 1
    assert "nover" in vios[0]


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
