# @feature: FP-0.2.PACK 打包/部署 CI 门 | @ci: python-test
"""check_ci_gate.py 单测（用户裁定 2026-10-28「CI 不过不打包不部署」）。

行为契约（断输入→输出，不钉实现）：
- 远端 slug 解析：github.com 的 https/ssh 形态出 owner/repo，gitee 等出 None；
- 「最近绿基线」判定：目标自身绿=过；祖先绿基线覆盖 paths 跳过的中间提交；
  基线后未压过的红=拦；基线后在跑=拦「等绿」；从未绿且最近红=拦；
  从未跑过=拦（fail-closed）；
- 被绿基线压过的历史红不拦（祖先修复语义）。
判定为纯函数（runs + 祖先谓词注入），不触网不触 git。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_ci_gate", SCRIPTS / "check_ci_gate.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass + future annotations 需模块在册
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _run(sha: str, status: str, conclusion: str | None, created: str) -> mod.RunInfo:
    return mod.RunInfo(
        workflow="env-gate.yml",
        head_sha=sha,
        status=status,
        conclusion=conclusion,
        created_at=created,
        html_url=f"https://ci/run/{sha[:6]}",
    )


def _always_true(_a: str, _b: str) -> bool:
    return True


def _ancestry(chain: dict[str, list[str]]):
    """注入式祖先谓词：chain[child] = 该提交的全部祖先（含自身）。"""

    def is_ancestor(a: str, b: str) -> bool:
        return a in chain.get(b, [])

    return is_ancestor


class TestParseRepoSlug:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("https://gitee.com/owner/repo.git", None),
            ("https://gitlab.com/owner/repo.git", None),
            ("not-a-url", None),
        ],
    )
    def test_slug(self, url: str, expected: str | None):
        assert mod.parse_repo_slug(url) == expected


class TestEvaluate:
    TARGET = "b" * 40

    def test_target_self_green_passes(self):
        runs = [_run(self.TARGET, "completed", "success", "2026-10-28T01:00:00Z")]
        v = mod.evaluate(runs, self.TARGET, _always_true)
        assert v.ok
        assert v.baseline is not None

    def test_ancestor_baseline_covers_path_skipped_commits(self):
        # paths 车道：绿基线在老提交，中间提交无运行 → 基线覆盖，放行
        old, mid, new = "a" * 40, "c" * 40, "d" * 40
        chain = {new: [old, mid, new]}
        runs = [
            _run(old, "completed", "success", "2026-10-27T01:00:00Z"),
            _run(new, "completed", "success", "2026-10-28T01:00:00Z"),
        ]
        v = mod.evaluate(runs, new, _ancestry(chain))
        assert v.ok

    def test_unpressed_failure_after_baseline_blocks(self):
        base, bad, new = "a" * 40, "f" * 40, "b" * 40
        chain = {new: [base, bad, new]}
        runs = [
            _run(base, "completed", "success", "2026-10-27T01:00:00Z"),
            _run(bad, "completed", "failure", "2026-10-27T12:00:00Z"),
        ]
        v = mod.evaluate(runs, new, _ancestry(chain))
        assert not v.ok
        assert "红" in v.reason

    def test_failure_superseded_by_later_green_passes(self):
        old_bad, fixed, new = "a" * 40, "f" * 40, "b" * 40
        chain = {new: [old_bad, fixed, new]}
        runs = [
            _run(old_bad, "completed", "failure", "2026-10-27T01:00:00Z"),
            _run(fixed, "completed", "success", "2026-10-27T12:00:00Z"),
        ]
        v = mod.evaluate(runs, new, _ancestry(chain))
        assert v.ok
        assert v.baseline is not None
        assert v.baseline.head_sha == fixed

    def test_inflight_after_baseline_blocks_as_waiting(self):
        base, running, new = "a" * 40, "r" * 40, "b" * 40
        chain = {new: [base, running, new]}
        runs = [
            _run(base, "completed", "success", "2026-10-27T01:00:00Z"),
            _run(running, "in_progress", None, "2026-10-28T00:00:00Z"),
        ]
        v = mod.evaluate(runs, new, _ancestry(chain))
        assert not v.ok
        assert "在跑" in v.reason

    def test_never_green_blocks_fail_closed(self):
        bad = "f" * 40
        runs = [_run(bad, "completed", "failure", "2026-10-28T01:00:00Z")]
        v = mod.evaluate(runs, bad, _always_true)
        assert not v.ok

    def test_no_runs_at_all_blocks(self):
        v = mod.evaluate([], self.TARGET, _always_true)
        assert not v.ok
        assert "未跑过" in v.reason

    def test_run_outside_ancestry_ignored(self):
        runs = [_run("x" * 40, "completed", "failure", "2026-10-28T01:00:00Z")]
        v = mod.evaluate(runs, self.TARGET, _ancestry({self.TARGET: [self.TARGET]}))
        assert v.ok is False  # 无本支运行 → 未跑过拦（fail-closed），异支红不误伤
        assert "未跑过" in v.reason


class TestVerdictContract:
    def test_reason_carries_run_url(self):
        runs = [_run("f" * 40, "completed", "failure", "2026-10-28T01:00:00Z")]
        v = mod.evaluate(runs, "f" * 40, _always_true)
        assert "https://ci/run/ffffff" in v.reason


class TestPackageFastSemantics:
    """用户裁定 2026-11-06「打包 CI = 改动相关 + 打包相关 + 一个功能验证」。"""

    def test_lane_keys_pinned_to_ci_changed_areas(self):
        """机械锁：LANE_FILTERS 键集 ≡ compute_lanes 输出键（防两处漂移）。"""
        import sys as _sys

        _sys.path.insert(0, str(SCRIPTS))
        from ci_changed_areas import compute_lanes

        lanes = compute_lanes(["kernel/src/foo.rs"])
        assert set(mod.LANE_FILTERS) <= set(lanes), "LANE_FILTERS 出现 compute_lanes 之外的键"

    def test_full_lane_splits_by_prefix_never_full_mode(self):
        """full 档按改动前缀细分，任何路径都不产出全量（用户裁定「只跑相关的」）。"""
        filters = mod.compute_package_filters({"full": True}, ["kernel/src/a.rs", "docs/x.md"])
        assert mod.FULL_MODE not in filters
        assert "kernel-fmt" in filters and "kernel-test" in filters
        assert mod.ALWAYS_FILTERS in filters

    def test_full_lane_scripts_prefix(self):
        filters = mod.compute_package_filters({"full": True}, ["scripts/foo.py"])
        assert "scripts-lint" in filters and "kernel-fmt" not in filters

    def test_full_lane_workflows_prefix(self):
        filters = mod.compute_package_filters({"full": True}, [".github/workflows/ci.yml"])
        assert "gate-wiring" in filters

    def test_mobile_and_deploy_lanes_mapped(self):
        """mobile/deploy 域车道映射（移动端/Linux 打包触发面对）。"""
        assert mod.LANE_FILTERS["mobile"] == "shell-config-sync"
        assert mod.LANE_FILTERS["deploy"] == "deploy-env"

    def test_full_prefix_filters_cover_mobile_deploy(self):
        prefixes = [p for p, _ in mod.FULL_PREFIX_FILTERS]
        assert "app-mobile/" in prefixes and "deploy/" in prefixes


class TestAttestationDelta:
    """取证缓存差集（用户裁定 2026-11-06「跑过了就不要重复跑」）。"""

    def test_delta_is_target_minus_green(self):
        assert mod.plan_delta({"a", "b", "c"}, {"b"}) == {"a", "c"}

    def test_fully_covered_yields_empty(self):
        assert mod.plan_delta({"a"}, {"a", "x"}) == set()

    def test_empty_green_runs_all(self):
        assert mod.plan_delta({"a", "b"}, set()) == {"a", "b"}

    def test_save_and_load_roundtrip(self, tmp_path):
        mod.save_green(tmp_path, "a" * 40, {"x", "y"})
        assert mod.load_green(tmp_path, "a" * 40) == {"x", "y"}

    def test_load_missing_returns_empty(self, tmp_path):
        assert mod.load_green(tmp_path, "b" * 40) == set()

    def test_load_sha_mismatch_returns_empty(self, tmp_path):
        mod.save_green(tmp_path, "c" * 40, {"x"})
        assert mod.load_green(tmp_path, "d" * 40) == set()

    def test_full_lane_without_files_falls_kernel(self):
        """无文件清单可细分时保守取 kernel 面（核心面最常见形态），仍非全量。"""
        filters = mod.compute_package_filters({"full": True})
        assert mod.FULL_MODE not in filters and "kernel-fmt" in filters

    def test_docs_only_yields_always_face(self):
        filters = mod.compute_package_filters({"full": False, "docs_only": True})
        assert filters == mod.ALWAYS_FILTERS

    def test_related_lane_composes_always_face(self):
        filters = mod.compute_package_filters({"full": False, "related": True})
        assert filters.startswith(mod.ALWAYS_FILTERS)
        assert "plugins-related-tests" in filters

    def test_frontend_lane_carries_frontend_filters(self):
        filters = mod.compute_package_filters({"full": False, "frontend": True})
        assert "frontend-typecheck" in filters and "frontend-knip-baseline" in filters

    def test_multi_lane_union_no_duplicate(self):
        filters = mod.compute_package_filters({"full": False, "related": True, "frontend": True})
        assert "plugins-related-tests" in filters and "frontend-typecheck" in filters


def _job(name: str, status: str, conclusion: str | None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": "https://ci/job"}


class TestEvaluatePackJob:
    """package kind 的 env-gate job 级判定（只认 Pack Windows；deploy-env 不拦打包）。"""

    def test_green_pack_job_passes(self):
        jobs = [
            _job("Deploy Env (部署环境检测 + 容器构建)", "completed", "failure"),
            _job("Pack Windows (打包链实测 + 静默安装冒烟)", "completed", "success"),
        ]
        v = mod.evaluate_pack_job(jobs)
        assert v.ok

    def test_red_pack_job_blocks_even_if_deploy_env_green(self):
        jobs = [
            _job("Deploy Env (部署环境检测 + 容器构建)", "completed", "success"),
            _job("Pack Windows (打包链实测 + 静默安装冒烟)", "completed", "failure"),
        ]
        v = mod.evaluate_pack_job(jobs)
        assert not v.ok

    def test_deploy_env_red_alone_does_not_block_packaging(self):
        jobs = [_job("Deploy Env (部署环境检测 + 容器构建)", "completed", "failure")]
        v = mod.evaluate_pack_job(jobs)
        assert not v.ok  # Pack Windows 缺席 = fail-closed，但 reason 指缺席而非 deploy-env 红
        assert "无 Pack Windows job" in v.reason

    def test_inflight_pack_job_blocks(self):
        jobs = [_job("Pack Windows (打包链实测 + 静默安装冒烟)", "in_progress", None)]
        v = mod.evaluate_pack_job(jobs)
        assert not v.ok
        assert "在跑" in v.reason

    def test_missing_pack_job_blocks_fail_closed(self):
        v = mod.evaluate_pack_job([])
        assert not v.ok
        assert "无法证明打包链实测" in v.reason


class TestRequiredWorkflowsByKind:
    def test_package_kind_requires_e2e(self):
        assert "e2e.yml" in mod.PACKAGE_REQUIRED
        assert "ci.yml" in mod.PACKAGE_REQUIRED and "env-gate.yml" in mod.PACKAGE_REQUIRED

    def test_deploy_kind_keeps_full_face(self):
        assert mod.REQUIRED_WORKFLOWS == ("ci.yml", "env-gate.yml")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
