# @feature: FP-0.2.ACP额度池 M0 | @ci: python-coverage
"""acp_seed 技能播种行为测试（on_load 载荷镜像）。

播种 = 插件载荷（SKILL.md/scripts/*）镜像到 ``<user_root>/skills/acp-quota-pool/``。
断言行级：覆盖写（真值同步）、缺载荷 fail-closed、降级入口三态可观察。
消费独名模块 acp_seed（bare `server` 跨插件碰撞实证在案，测试不 import server）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from acp_seed import PAYLOAD_FILES, SKILL_ID, seed_skill_into, seed_with_degradation

pytestmark = pytest.mark.unit

_PAYLOAD = tuple(PAYLOAD_FILES)  # 夹具按真实载荷清单播种（消费公开真值，不私抄）


def _make_plugin_fixture(tmp_path: Path) -> Path:
    plugin_dir = tmp_path / "plugin"
    for rel in _PAYLOAD:
        target = plugin_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"payload-of-{rel}", encoding="utf-8")
    return plugin_dir


def test_seed_copies_payload_into_user_root_skills(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_fixture(tmp_path)
    user_root = tmp_path / "user_root"

    written = seed_skill_into(user_root, plugin_dir)

    assert sorted(written) == sorted(_PAYLOAD)
    for rel in _PAYLOAD:
        seeded = user_root / "skills" / SKILL_ID / rel
        assert seeded.is_file()
        assert seeded.read_text(encoding="utf-8") == f"payload-of-{rel}"


def test_seed_overwrites_stale_copy_as_truth_sync(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_fixture(tmp_path)
    user_root = tmp_path / "user_root"
    stale = user_root / "skills" / SKILL_ID / "SKILL.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale-content", encoding="utf-8")

    seed_skill_into(user_root, plugin_dir)

    assert stale.read_text(encoding="utf-8") == "payload-of-SKILL.md"


def test_seed_missing_payload_fails_closed(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "empty_plugin"
    plugin_dir.mkdir()
    user_root = tmp_path / "user_root"

    with pytest.raises(OSError, match="载荷缺失"):
        seed_skill_into(user_root, plugin_dir)


def test_seed_target_layout_matches_workspace_skill_contract(tmp_path: Path) -> None:
    """播种后布局 = skills/<SKILL_ID>/scripts/acp_pool.py（bash 相对可达契约）。"""
    plugin_dir = _make_plugin_fixture(tmp_path)
    user_root = tmp_path / "user_root"

    seed_skill_into(user_root, plugin_dir)

    assert (user_root / "skills" / SKILL_ID / "scripts" / "acp_engine.py").is_file()


def test_degradation_entry_reports_three_observable_states(tmp_path: Path) -> None:
    """降级入口三态：成功 / user_root 不可得 skip / 失败 degraded——不抛。"""
    plugin_dir = _make_plugin_fixture(tmp_path)

    assert seed_with_degradation(tmp_path / "user_root", plugin_dir) == "seeded: 6 files"
    assert seed_with_degradation(None, plugin_dir).startswith("skipped:")
    degraded = seed_with_degradation(tmp_path / "user_root", tmp_path / "empty_plugin")
    assert degraded.startswith("degraded:")
