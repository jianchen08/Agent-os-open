# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""runner 语义测试（设计稿 §3.3）：契约校验 fail-closed、lane 过滤、tier block/warn、
子进程隔离、退出码契约、staged stdin、local 并读覆盖、last_run.json 留痕。

runner 用真实 packs/ci/base/run.py（关键路径走真实依赖），checker 用假件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ci_packs.conftest import (
    CHECKER_CRASH,
    CHECKER_FAIL,
    CHECKER_OK,
    CHECKER_SKIP,
    CHECKER_STDIN,
    load_runner_module,
    make_ci_project,
    run_runner,
)

pytestmark = pytest.mark.unit


def _gate(gate_id: str, check: str, tier: str = "block", lane: str = "commit") -> dict:
    return {
        "id": gate_id,
        "lane": lane,
        "tier": tier,
        "run": ["python", f"checks/{check}.py"],
        "est_seconds": 1,
    }


def _last_run(project: Path) -> dict:
    return json.loads((project / ".agentos" / "ci" / "last_run.json").read_text(encoding="utf-8"))


def _result_by_id(project: Path, gate_id: str) -> dict:
    results = _last_run(project)["results"]
    return next(r for r in results if r["id"] == gate_id)


class TestContractValidation:
    """元 CI ①（J9）：声明非法 → 红 + 报出字段，fail-closed。"""

    def test_missing_id_reported(self, tmp_path: Path) -> None:
        bad = {"lane": "commit", "tier": "block", "run": ["python", "checks/ok.py"], "est_seconds": 1}
        project = make_ci_project(tmp_path, [bad], {"ok": CHECKER_OK})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert "id" in r.stdout
        assert (_last_run(project))["red"] is True

    def test_missing_run_reported(self, tmp_path: Path) -> None:
        bad = {"id": "g1", "lane": "commit", "tier": "block", "est_seconds": 1}
        project = make_ci_project(tmp_path, [bad], {})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert "run" in r.stdout

    def test_tier_outside_enum_reported(self, tmp_path: Path) -> None:
        bad = {"id": "g1", "lane": "commit", "tier": "banana", "run": ["python", "checks/ok.py"], "est_seconds": 1}
        project = make_ci_project(tmp_path, [bad], {"ok": CHECKER_OK})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert "tier" in r.stdout

    def test_missing_checker_file_reported(self, tmp_path: Path) -> None:
        gate = _gate("g1", "ghost")
        project = make_ci_project(tmp_path, [gate], {})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert "ghost" in r.stdout

    def test_local_face_violation_also_red(self, tmp_path: Path) -> None:
        """local 面是项目自治，但契约校验同样执法（元 CI 不豁免 local）。"""
        project = make_ci_project(
            tmp_path,
            [_gate("factory_ok", "ok")],
            {"ok": CHECKER_OK},
            local_gates=[{"id": "local_bad", "lane": "commit", "tier": "warn", "run": ["python"]}],
        )
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert "run" in r.stdout

    def test_valid_gates_pass_contract_in_process(self, tmp_path: Path) -> None:
        runner = load_runner_module()
        project = make_ci_project(tmp_path, [], {"ok": CHECKER_OK})
        gates = [_gate("g1", "ok"), _gate("g2", "ok", tier="warn", lane="manual")]
        assert runner.validate_gates(gates, "factory", project / ".agentos" / "ci") == []


class TestLaneAndTier:
    def test_manual_lane_not_run_in_commit(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker.txt"
        manual_gate = {
            "id": "manual_g",
            "lane": "manual",
            "tier": "block",
            "run": ["python", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('x')", str(marker)],
            "est_seconds": 1,
        }
        project = make_ci_project(tmp_path, [manual_gate], {})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 0
        assert not marker.exists()

    def test_block_gate_fail_blocks(self, tmp_path: Path) -> None:
        project = make_ci_project(tmp_path, [_gate("g1", "f")], {"f": CHECKER_FAIL})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert "违规X" in r.stdout  # stdout=发现清单
        assert _result_by_id(project, "g1")["status"] == "fail"

    def test_warn_gate_fail_does_not_block(self, tmp_path: Path) -> None:
        project = make_ci_project(tmp_path, [_gate("w1", "f", tier="warn")], {"f": CHECKER_FAIL})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 0
        assert "违规X" in r.stdout  # 只报不拦，但可见
        assert _result_by_id(project, "w1")["status"] == "fail"

    def test_skip_does_not_block_and_records_reason(self, tmp_path: Path) -> None:
        project = make_ci_project(tmp_path, [_gate("s1", "s")], {"s": CHECKER_SKIP})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 0
        result = _result_by_id(project, "s1")
        assert result["status"] == "skip"
        assert "工具缺席" in json.dumps(result, ensure_ascii=False)

    def test_all_green_passes(self, tmp_path: Path) -> None:
        project = make_ci_project(tmp_path, [_gate("g1", "ok"), _gate("g2", "ok2")], {"ok": CHECKER_OK, "ok2": CHECKER_OK})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 0
        assert _last_run(project)["red"] is False


class TestSubprocessIsolation:
    def test_checker_crash_does_not_kill_runner(self, tmp_path: Path) -> None:
        """checker 崩溃（非 0/1/2 退出码）≠ runner 崩溃：记 error，block 门红。"""
        project = make_ci_project(tmp_path, [_gate("c1", "crash"), _gate("g2", "ok")], {"crash": CHECKER_CRASH, "ok": CHECKER_OK})
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1  # runner 活着并汇总红
        assert _result_by_id(project, "c1")["status"] == "error"
        assert _result_by_id(project, "g2")["status"] == "pass"

    def test_warn_tier_crash_does_not_block(self, tmp_path: Path) -> None:
        project = make_ci_project(tmp_path, [_gate("c1", "crash", tier="warn")], {"crash": CHECKER_CRASH})
        assert run_runner(project, "--lane", "commit").returncode == 0


class TestStagedStdin:
    def test_staged_list_passed_via_stdin(self, tmp_path: Path) -> None:
        """staged 清单经 stdin 传入 checker（hook 用 -z NUL 分隔）。"""
        project = make_ci_project(tmp_path, [_gate("g1", "stdin")], {"stdin": CHECKER_STDIN})
        r = run_runner(project, "--lane", "commit", "--staged", stdin=b"a.py\x00b b.py")
        assert r.returncode == 1
        assert "STAGED:a.py,b b.py" in r.stdout

    def test_staged_parse_newline_fallback(self, tmp_path: Path) -> None:
        runner = load_runner_module()
        assert runner.parse_staged("a.py\nb.py\n") == ["a.py", "b.py"]
        assert runner.parse_staged("a.py\x00b.py\x00") == ["a.py", "b.py"]
        assert runner.parse_staged("") == []


class TestLocalExtensionFace:
    def test_local_gate_runs_without_reassembly(self, tmp_path: Path) -> None:
        """J7 生长闭环：local/gates.json 补门（checker 放 local/ 同目录）→ 下次运行即执法。"""
        local_gate = {
            "id": "local_reg",
            "lane": "commit",
            "tier": "block",
            "run": ["python", "local/checks/lfail.py"],
            "est_seconds": 1,
        }
        project = make_ci_project(
            tmp_path,
            [_gate("g1", "ok")],
            {"ok": CHECKER_OK},
            local_gates=[local_gate],
            local_checks={"checks/lfail": CHECKER_FAIL},
        )
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 1
        assert _result_by_id(project, "local_reg")["status"] == "fail"
        assert _result_by_id(project, "local_reg")["source"] == "local"

    def test_local_same_id_overrides_factory_with_banner(self, tmp_path: Path) -> None:
        """BuiltinFirst 同构：local 与工厂同 id → local 生效 + warn 横幅。"""
        project = make_ci_project(
            tmp_path,
            [_gate("dup", "f")],
            {"f": CHECKER_FAIL, "ok": CHECKER_OK},
            local_gates=[_gate("dup", "ok")],
        )
        r = run_runner(project, "--lane", "commit")
        assert r.returncode == 0  # local 的 ok 赢了工厂的 fail
        assert "覆盖" in r.stdout
        assert _last_run(project)["warnings"]
        results = _last_run(project)["results"]
        assert len([r for r in results if r["id"] == "dup"]) == 1
        assert results[0]["source"] == "local"


class TestLastRun:
    def test_last_run_records_time_lane_and_durations(self, tmp_path: Path) -> None:
        project = make_ci_project(tmp_path, [_gate("g1", "ok")], {"ok": CHECKER_OK})
        run_runner(project, "--lane", "commit")
        data = _last_run(project)
        assert data["lane"] == "commit"
        assert data["time"]
        assert data["results"][0]["duration_ms"] >= 0
        assert data["results"][0]["exit_code"] == 0
