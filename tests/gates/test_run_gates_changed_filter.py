# @feature: FP-GATE fast 档变更模块过滤 | @ci: python-coverage
"""run_gates fast 档变更模块过滤回归（方案 §3.6 步骤 5：fast 档变更模块过滤）。

断行为口径：
- Gate.paths 守护路径语义（空 = cross-cutting 恒跑；非空 = 前缀命中才跑）；
- 过滤不变量：--changed-files 结果集 ⊆ 未过滤 fast 全集（只缩不扩，防漏检方向）；
- 新 gate 注册（gate-protected / enforcement-tier）与 fast 元数据完备；
- CLI 合同（--list --json 含 paths；--dry-run 可预演选择）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_gates.py"
_spec = importlib.util.spec_from_file_location("run_gates_under_test", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("run_gates_under_test", _mod)
_spec.loader.exec_module(_mod)

Gate = _mod.Gate
filter_by_changed = _mod.filter_by_changed
GATES = _mod.GATES


def _gate(paths: tuple[str, ...] = (), gid: str = "g") -> Gate:
    return Gate(id=gid, label=gid, domain="cross", command=("true",), est_seconds=1, paths=paths)


class TestGatePathsField:
    def test_paths_defaults_to_empty(self) -> None:
        assert _gate().paths == ()

    def test_paths_accepts_tuple(self) -> None:
        g = _gate(paths=("kernel/",), gid="k")
        assert g.paths == ("kernel/",)


class TestFilterByChanged:
    def test_empty_paths_gate_always_kept(self) -> None:
        g = _gate()
        assert filter_by_changed([g], []) == [g]  # 空变更集也恒跑
        assert filter_by_changed([g], ["a.py", "b/c.md"]) == [g]

    def test_prefix_match_decides_kept_or_dropped(self) -> None:
        g = _gate(paths=("plugins/",), gid="p")
        kept = filter_by_changed([g], ["plugins/shared/x/server.py", "docs/a.md"])
        assert kept == [g]
        dropped = filter_by_changed([g], ["docs/a.md", "kernel/src/lib.rs"])
        assert dropped == []

    def test_subset_invariant_filter_only_shrinks(self) -> None:
        """性质断言：任意组合下过滤结果 ⊆ 全集（漏检方向的硬约束）。

        用列表包含而非 set：frozen dataclass 含 dict 字段不可哈希，
        `in` 走 __eq__ 语义等价且不依赖 hash。
        """
        gates = [
            _gate(),
            _gate(paths=("kernel/",), gid="k"),
            _gate(paths=("docs/",), gid="d"),
            _gate(paths=("scripts/", "config/"), gid="s"),
        ]
        for changed in (
            [],
            ["a.py"],
            ["kernel/x.rs", "docs/y.md"],
            ["config/z.yaml", "unrelated.txt"],
            ["kernel/", "scripts/a.py", "docs/b.md", "other.py"],
        ):
            result = filter_by_changed(gates, list(changed))
            assert all(g in gates for g in result)

    def test_no_changed_arg_is_identity(self) -> None:
        """未传变更集（CI/既有用法）行为完全不变——恒等返回。"""
        gates = [_gate(paths=("kernel/",), gid="k"), _gate()]
        assert filter_by_changed(gates, None) == gates


class TestNewGatesRegistered:
    def test_protected_and_tier_gates_registered_and_fast(self) -> None:
        by_id = {g.id: g for g in GATES}
        for gid in ("gate-protected", "enforcement-tier"):
            assert gid in by_id, f"gate {gid} 未注册"
            g = by_id[gid]
            assert g.fast is True  # 提交即闸必须在 fast 档
            assert g.est_seconds is not None  # V5 预算要求
            assert g.wiring == "ci"  # 接线（V1 由 ci.yml --filter 引用）

    def test_gate_protected_is_cross_cutting(self) -> None:
        """受保护清单检查自己必须恒跑（不得被 paths 过滤掉）。"""
        by_id = {g.id: g for g in GATES}
        assert by_id["gate-protected"].paths == ()


class TestFastPathsSpotCheck:
    """抽检确定性标注的 fast gate 守护路径（保守标注：不确定的留空恒跑）。"""

    def test_spot_check_known_paths(self) -> None:
        by_id = {g.id: g for g in GATES if g.fast}
        assert by_id["kernel-fmt"].paths == ("kernel/",)
        assert by_id["graph-check"].paths == ("docs/",)


class TestCLIContract:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Windows 控制台默认 GBK：既有 gate label 含非 GBK 字符（↔），
        # 父子进程双侧显式 UTF-8 与 CI（Linux）口径对齐。
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
        )

    def test_list_json_exposes_paths(self) -> None:
        proc = self._run("--list", "--json")
        assert proc.returncode == 0, proc.stderr
        gates = json.loads(proc.stdout)
        assert all("paths" in g for g in gates), "CLI JSON 合同缺 paths 字段"

    def test_dry_run_filters_by_changed_files(self) -> None:
        # frontend 变更：命中 frontend 系 gate，不命中 kernel 系
        proc = self._run(
            "--mode", "fast", "--changed-files", "frontend/src/x.ts", "--dry-run"
        )
        assert proc.returncode == 0, proc.stderr
        assert "frontend-theme-color-hygiene" in proc.stdout
        assert "kernel-fmt" not in proc.stdout

        # kernel 变更：命中 kernel-fmt
        proc2 = self._run(
            "--mode",
            "fast",
            "--changed-files",
            "kernel/crates/core/src/lib.rs",
            "--dry-run",
        )
        assert proc2.returncode == 0, proc2.stderr
        assert "kernel-fmt" in proc2.stdout

    def test_dry_run_always_keeps_gate_protected(self) -> None:
        """受保护清单检查恒跑：无论变更集打到哪里。"""
        proc = self._run(
            "--mode",
            "fast",
            "--changed-files",
            "some/unrelated/file.txt",
            "--dry-run",
        )
        assert proc.returncode == 0, proc.stderr
        assert "gate-protected" in proc.stdout


class TestGateTmpIsolation:
    """门禁临时目录按 run 隔离（并发 run 不互删 tee 文件）。"""

    def test_run_tmp_dir_namespaced_by_run_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        run_dir = _mod._run_tmp_dir()
        assert _mod._RUN_ID in run_dir.name
        assert run_dir.parent == Path(".zctmp") / "gates"

    def test_cleanup_only_removes_own_run_dir(self, tmp_path, monkeypatch) -> None:
        """清理只删本 run 目录；同父目录下其它 run 的产物必须保留。"""
        monkeypatch.chdir(tmp_path)
        own = _mod._run_tmp_dir()
        own.mkdir(parents=True)
        (own / "gate_tmp.own").write_text("own", encoding="utf-8")
        sibling = _mod._GATE_TMP_ROOT / "other-run"
        sibling.mkdir(parents=True)
        (sibling / "gate_tmp.other").write_text("other", encoding="utf-8")
        _mod._cleanup_gate_tmp()
        assert not own.exists()
        assert (sibling / "gate_tmp.other").is_file()
