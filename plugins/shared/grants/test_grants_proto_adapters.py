# @feature: FP-0.2.grants_ledger 授权账本 | @ci: python-coverage
"""grants proto-grant 只读投影（proto_adapters.py）测试——API 契约由本文件定义（TDD Red 先行）。

完成判据 4（执行方案 §1）：构造 permanent_whitelist.json 样例 → 投影为 standing grant
→ 删除该条 → 投影消失。
另覆盖：管道级免审批投影、写区授权 loader 桩（真源经服务可达性留 P1 接线，
本批接口先行）、源文件缺失投影为空、统一 project_all。

设计真值：ADR D3（只读投影、存储不动）+ 执行方案 §8（投影规格表）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
for _p in (_SHARED_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import proto_adapters as grants_proto  # noqa: E402 — 平铺插件模块

project_all = grants_proto.project_all
project_pipeline_modes = grants_proto.project_pipeline_modes
project_whitelist = grants_proto.project_whitelist
project_zone_grants = grants_proto.project_zone_grants


def _write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class TestWhitelistProjection:
    """完成判据 4：白名单样例 → standing grant → 删条 → 投影消失。"""

    def test_whitelist_sample_projects_then_entry_deleted_projection_gone(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "permanent_whitelist.json", ["git status", "npm run test"])
        grants = project_whitelist(str(src))
        assert len(grants) == 2
        git_grant = next(g for g in grants if g["tool"] == "git status")
        # 执行方案 §8 投影形态：granularity=standing、tool=命令指纹
        assert git_grant["granularity"] == "standing"
        assert git_grant["status"] == "active"
        assert git_grant["source"] == "proto_projection"
        assert git_grant["grant_id"].startswith("proto:command_whitelist:")
        assert git_grant["scope"] == {"kind": "command_signature"}

        # 删除该条（真源 JSON 删改）→ 下次查询实时读源，投影即时消失
        remaining = _write_json(tmp_path / "permanent_whitelist.json", ["npm run test"])
        grants_after = project_whitelist(str(remaining))
        assert [g["tool"] for g in grants_after] == ["npm run test"]
        assert all(g["grant_id"] != git_grant["grant_id"] for g in grants_after)

    def test_whitelist_ids_stable_per_signature(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "permanent_whitelist.json", ["git status"])
        first = project_whitelist(str(src))[0]["grant_id"]
        second = project_whitelist(str(src))[0]["grant_id"]
        assert first == second

    def test_whitelist_source_file_missing_projects_empty(self, tmp_path: Path) -> None:
        assert project_whitelist(str(tmp_path / "absent.json")) == []

    def test_whitelist_malformed_entries_skipped(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "permanent_whitelist.json", ["git status", "", "   ", 42, None])
        grants = project_whitelist(str(src))
        assert [g["tool"] for g in grants] == ["git status"]

    def test_whitelist_malformed_source_projects_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "broken.json"
        src.write_text("{not json", encoding="utf-8")
        assert project_whitelist(str(src)) == []


class TestPipelineModesProjection:
    def test_pipeline_mode_projects_as_task_grant(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "permission_modes_pipeline.json", {"pipe-1": "bypass"})
        grants = project_pipeline_modes(str(src))
        assert len(grants) == 1
        g = grants[0]
        # 执行方案 §8 投影形态：subject_kind=pipeline、granularity=task、tool='*'
        assert g["subject_kind"] == "pipeline"
        assert g["subject_id"] == "pipe-1"
        assert g["granularity"] == "task"
        assert g["tool"] == "*"
        assert g["action"] == "*"
        assert g["source"] == "proto_projection"
        assert g["scope"]["mode"] == "bypass"

    def test_pipeline_mode_entry_removed_projection_gone(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "permission_modes_pipeline.json", {"pipe-1": "bypass"})
        assert len(project_pipeline_modes(str(src))) == 1
        _write_json(tmp_path / "permission_modes_pipeline.json", {})
        assert project_pipeline_modes(str(src)) == []

    def test_pipeline_mode_source_missing_projects_empty(self, tmp_path: Path) -> None:
        assert project_pipeline_modes(str(tmp_path / "absent.json")) == []

    def test_pipeline_mode_malformed_entries_skipped(self, tmp_path: Path) -> None:
        src = _write_json(
            tmp_path / "permission_modes_pipeline.json",
            {"": "bypass", "pipe-ok": "bypass", "pipe-bad": "weird"},
        )
        grants = project_pipeline_modes(str(src))
        # 空 pipeline_id 与表外档位值跳过（投影只收缩授权面）；JSON 键恒为 str，
        # 非 str 键防御分支不经 JSON 形态触发
        assert [g["subject_id"] for g in grants] == ["pipe-ok"]


class TestZoneProjection:
    """写区授权：真源经 project 服务才可达（ADR 2026-09-24），接口先行 + 桩测试。

    禁为它硬编码路径——loader 由 P1 接线注入，本批只定契约。
    """

    def test_zone_loader_stub_projects_resource_pattern(self) -> None:
        entries = [
            {"path": "D:/workspaces/task-1/**", "pipeline_id": "pipe-9"},
            {"path": "V:/shared/**"},
        ]
        grants = project_zone_grants(loader=lambda: entries)
        assert len(grants) == 2
        first = grants[0]
        assert first["scope"]["resource_pattern"] == "D:/workspaces/task-1/**"
        assert first["subject_id"] == "pipe-9"
        assert first["source"] == "proto_projection"
        assert first["proto_kind"] == "write_zone"

    def test_zone_loader_none_projects_empty(self) -> None:
        assert project_zone_grants(loader=None) == []

    def test_zone_loader_missing_path_key_skipped(self) -> None:
        grants = project_zone_grants(loader=lambda: [{"pipeline_id": "p-1"}])
        assert grants == []

    def test_zone_loader_raising_projects_empty(self) -> None:
        def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("project service unreachable")

        assert project_zone_grants(loader=_boom) == []

    def test_zone_loader_non_dict_entries_skipped(self) -> None:
        grants = project_zone_grants(loader=lambda: ["D:/ws/**", {"path": "D:/ws2/**"}])
        assert [g["scope"]["resource_pattern"] for g in grants] == ["D:/ws2/**"]


class TestProjectAll:
    def test_project_all_merges_three_sources(self, tmp_path: Path) -> None:
        wl = _write_json(tmp_path / "permanent_whitelist.json", ["git status"])
        pm = _write_json(tmp_path / "permission_modes_pipeline.json", {"pipe-1": "bypass"})
        grants = project_all(
            whitelist_file=str(wl),
            pipeline_modes_file=str(pm),
            zone_loader=lambda: [{"path": "D:/ws/**", "pipeline_id": "pipe-1"}],
        )
        kinds = {g["proto_kind"] for g in grants}
        assert kinds == {"command_whitelist", "pipeline_mode", "write_zone"}
        assert all(g["source"] == "proto_projection" for g in grants)

    def test_project_all_defaults_empty(self) -> None:
        assert project_all() == []

    def test_projections_are_pure_dicts_without_db_state(self, tmp_path: Path) -> None:
        # 只读投影不写任何库——proto_grant 不落 grants.db（ADR D3 存储不动）
        src = _write_json(tmp_path / "permanent_whitelist.json", ["git status"])
        grants = project_all(whitelist_file=str(src))
        assert grants
        assert not (tmp_path / "grants.db").exists()
        assert all(g.get("uses") == 0 for g in grants)
