# @feature: FP-0.2.内核内存治理 G3a 复现车道脚手架 | @ci: python-coverage
# @feature: G3 复现车道脚手架 | @ci: python-coverage
"""memory_storm_replay 脚本契约测试。

锁车道核心硬行为：参数组合校验边界、清单发现（坏 JSON 跳过/嵌套收编）、
微改语义（JSON 等价 + 字节必不同）、快照/还原闭环（含强删重建）、CSV 行
形状（None/缺失落空串）、以及 --self-test 干跑全绿。网络面（登录/采样）
不在单测面（外部依赖，车道实测覆盖）。

加载经 importlib 唯名模块（scripts/ 非包，防裸名串扰，与
test_sync_installed_user_space_script.py 同款）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_storm_replay.py"


@pytest.fixture
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("memory_storm_replay_test_mod", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["memory_storm_replay_test_mod"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def plugin_tree(tmp_path: Path) -> tuple[Path, list[Path]]:
    """目标插件配置根与期望清单列表（坏 JSON 清单与干扰文件应被跳过）。"""
    root = tmp_path / "plugins"
    (root / "p1").mkdir(parents=True)
    (root / "p1" / "plugin.json").write_text('{"id": "p1"}', encoding="utf-8")
    (root / "deep" / "nested").mkdir(parents=True)
    (root / "deep" / "nested" / "plugin.json").write_text('{"id": "deep"}', encoding="utf-8")
    (root / "broken").mkdir()
    (root / "broken" / "plugin.json").write_text("not json {", encoding="utf-8")
    (root / "not_a_manifest.json").write_text("{}\n", encoding="utf-8")
    manifests = [
        root / "deep" / "nested" / "plugin.json",
        root / "p1" / "plugin.json",
    ]
    return root, manifests


# ── 参数组合校验 ────────────────────────────────────────────────────────────


def test_validate_args_rejects_missing_password(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    assert mod.validate_args(plugin_tree[0], 100, 30, 5.0, None) is not None


def test_validate_args_rejects_missing_target_dir(mod: Any) -> None:
    assert mod.validate_args(None, 100, 30, 5.0, "pw") is not None


def test_validate_args_rejects_nonexistent_target_dir(mod: Any, tmp_path: Path) -> None:
    assert mod.validate_args(tmp_path / "nope", 100, 30, 5.0, "pw") is not None


def test_validate_args_rejects_nonpositive_rates(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    target = plugin_tree[0]
    assert mod.validate_args(target, 0, 30, 5.0, "pw") is not None
    assert mod.validate_args(target, 100, 0, 5.0, "pw") is not None
    assert mod.validate_args(target, 100, 30, 0.0, "pw") is not None


def test_validate_args_accepts_valid_combo(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    target = plugin_tree[0]
    assert mod.validate_args(target, 100, 30, 5.0, "pw") is None


# ── 清单发现 ────────────────────────────────────────────────────────────────


def test_discover_manifests_exact_match(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    root, manifests = plugin_tree
    assert mod.discover_manifests(root) == manifests


def test_discover_manifests_empty_dir(mod: Any, tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    assert mod.discover_manifests(root) == []


# ── 微改语义 ────────────────────────────────────────────────────────────────


def test_mutate_keeps_json_semantics_and_changes_bytes(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    original = plugin_tree[1][0].read_bytes()
    for seq in range(6):
        mutated = mod.mutate_manifest_bytes(original, seq)
        assert json.loads(mutated) == json.loads(original), f"seq={seq} 语义必须等价"
        assert mutated != original, f"seq={seq} 字节必须不同"


def test_mutate_survives_cjk_and_nested_content(mod: Any) -> None:
    original = '{"id": "示例", "persona": {"lines": ["你好", "世界"]}}'.encode()
    mutated = mod.mutate_manifest_bytes(original, 1)
    assert json.loads(mutated.decode()) == json.loads(original.decode())
    assert mutated != original


def test_mutate_alternates_serialization_shapes(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    original = plugin_tree[1][0].read_bytes()
    shapes = {mod.mutate_manifest_bytes(original, seq) for seq in range(4)}
    assert len(shapes) >= 2, "不同 seq 应产生不同序列化形态（轮转覆盖 mtime/hash 两类判定）"


# ── 快照/还原闭环 ───────────────────────────────────────────────────────────


def test_storm_write_changes_bytes_and_restore_recovers(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    originals = {p: p.read_bytes() for p in plugin_tree[1]}
    storm = mod.StormReplay(list(plugin_tree[1]), seed=7)

    touched = storm.write_once()
    assert touched in originals
    assert touched.read_bytes() != originals[touched], "风暴写入必须改变字节"

    assert storm.restore_all() == []
    assert touched.read_bytes() == originals[touched], "还原必须恢复原字节"
    assert all(p.read_bytes() == originals[p] for p in plugin_tree[1]), "全树回到原状"


def test_restore_recreates_deleted_file(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    originals = {p: p.read_bytes() for p in plugin_tree[1]}
    storm = mod.StormReplay(list(plugin_tree[1]), seed=7)
    touched = storm.write_once()

    touched.unlink()
    assert storm.restore_all() == []
    assert touched.exists(), "删除文件必须被重建"
    assert touched.read_bytes() == originals[touched]


def test_storm_replay_rejects_empty_manifests(mod: Any) -> None:
    with pytest.raises(mod.LaneError):
        mod.StormReplay([], seed=0)


def test_storm_seq_round_robin_covers_all_manifests(mod: Any, plugin_tree: tuple[Path, list[Path]]) -> None:
    manifests = plugin_tree[1]
    storm = mod.StormReplay(list(manifests), seed=7)
    for _ in range(len(manifests)):
        storm.write_once()
    assert storm.touched_count() == len(manifests), "轮转一轮必须覆盖全部清单"


# ── CSV 行形状 ──────────────────────────────────────────────────────────────


def test_build_csv_row_field_order_and_blank_fallback(mod: Any) -> None:
    row = mod.build_csv_row(
        1.5, {"committed_bytes": 100, "abandoned_pages": None}, "net down"
    )
    assert list(row) == mod.CSV_FIELDS
    assert row["utc_ts"], "utc_ts 必须非空"
    assert row["elapsed_s"] == "1.5"
    assert row["committed_bytes"] == 100
    assert row["abandoned_pages"] == "", "None 必须落空串"
    assert row["process_commit_bytes"] == "", "缺失字段必须落空串"
    assert row["process_rss_bytes"] == ""
    assert row["sample_error"] == "net down"


def test_build_csv_row_preserves_zero(mod: Any) -> None:
    row = mod.build_csv_row(0.0, {"abandoned_pages": 0}, "")
    assert row["abandoned_pages"] == 0, "0 是有效观测值，不得吞成空串"


# ── 自检与 CLI 编排 ─────────────────────────────────────────────────────────


def test_self_test_end_to_end_green(mod: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert mod.main(["--self-test"]) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "全部通过" in out


def test_main_requires_out_for_lane(mod: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert mod.main([]) == 2
    assert "--out" in capsys.readouterr().err
