# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 外部公开数据集接入 | @ci: python-coverage
# @feature: 外部公开数据集接入（ADR 2026-09-16-external-dataset-sourcing） | @ci: python-coverage
"""external_datasets 纯函数单测：难度分带/分层种子/oracle 改造/许可画像/轨迹池映射。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import external_datasets as ext  # noqa: E402


def _swe_row(iid="django__django-1", repo="django/django", difficulty="<15 min fix",
             issue="Auth leak when * MagicMixin 遇到特殊字符。", n_f2p=2):
    return {
        "instance_id": iid, "repo": repo, "difficulty": difficulty,
        "base_commit": "abc1234", "environment_setup_commit": "def5678",
        "created_at": "2023-01-01", "version": "4.0",
        "problem_statement": issue, "patch": "diff --git a" * 3,
        "test_patch": "diff --git t", "FAIL_TO_PASS": json.dumps([f"t{i}" for i in range(n_f2p)]),
        "PASS_TO_PASS": json.dumps(["tp1"]),
    }


# ── 难度分带 ─────────────────────────────────────────────────────────────
def test_difficulty_band_mapping():
    assert ext.difficulty_band("<15 min fix") == "easy"
    assert ext.difficulty_band("15 min - 1 hour") == "medium"
    assert ext.difficulty_band("1-4 hours") == "hard"
    assert ext.difficulty_band(">4 hours") == "hard"  # 样本稀少档并入 hard
    assert ext.difficulty_band("未知档") == ""
    assert ext.difficulty_band(None) == ""


# ── 分层种子选取 ─────────────────────────────────────────────────────────
def test_seed_selection_covers_bands_with_quota():
    rows = ([_swe_row(f"d__{i}", difficulty="<15 min fix") for i in range(10)]
            + [_swe_row(f"m__{i}", difficulty="15 min - 1 hour") for i in range(10)]
            + [_swe_row(f"h__{i}", difficulty="1-4 hours") for i in range(10)])
    seed = ext.select_seed(rows, per_band=5)
    assert {b: len(v) for b, v in seed.items()} == {"easy": 5, "medium": 5, "hard": 5}
    all_ids = [r["instance_id"] for v in seed.values() for r in v]
    assert len(set(all_ids)) == 15  # 带间不重复


def test_seed_selection_spreads_across_repos_before_reuse():
    rows = [_swe_row(f"a__{i}", repo="a/a", difficulty="<15 min fix") for i in range(10)]
    rows += [_swe_row(f"b__{i}", repo="b/b", difficulty="<15 min fix") for i in range(10)]
    seed = ext.select_seed(rows, per_band=4)
    repos = [r["repo"] for r in seed["easy"]]
    assert repos == ["a/a", "b/b", "a/a", "b/b"]  # 轮转而非同仓连取


def test_seed_selection_skips_unknown_band_and_oversized_issues():
    rows = [_swe_row("x1", difficulty="<15 min fix", issue="短题面"),
            _swe_row("x2", difficulty="<15 min fix", issue="字" * 2000),
            _swe_row("x3", difficulty="神秘档位")]
    seed = ext.select_seed(rows, per_band=5)
    assert [r["instance_id"] for r in seed["easy"]] == ["x1"]
    assert seed["hard"] == [] and seed["medium"] == []


def test_seed_selection_is_deterministic():
    rows = [_swe_row(f"r{i}", repo=f"repo{i % 3}/x", difficulty="1-4 hours")
            for i in range(12)]
    once = [r["instance_id"] for r in ext.select_seed(rows, per_band=5)["hard"]]
    twice = [r["instance_id"] for r in ext.select_seed(rows, per_band=5)["hard"]]
    assert once == twice and len(once) == 5


# ── 任务改造（oracle=测试车道） ──────────────────────────────────────────
def test_adapt_seed_case_shape_and_oracle():
    row = _swe_row("django__django-16377", issue="Resolve this issue.", n_f2p=2)
    case = ext.adapt_seed_case(row, materials_root="/mat", mode="coding")
    assert case["id"] == "swev_django__django-16377"
    # 评测单元=任务链：target 锚定模式链 L2 编排入口（运行时注册表命名
    # 空间 id，裸 config_id 不被 task_submit 解析——实跑 400 实证）
    assert case["target"] == "mode_coding/programming_orchestrator_agent_v2"
    assert ext.chain_target("research") == "general_agent"  # 无注册编排，落回通用执行者
    assert ext.chain_target("general") == "general_agent"
    assert case["anchor"] == "repo" and case["workspace_mode"] == "worktree"
    assert case["workspace"] == "/mat/external/swe_repos/django__django-16377"
    assert "Resolve this issue." in case["messages"][0]  # issue 原文进题面
    assert "django/django@abc1234" in case["messages"][0]
    cmd = case["acceptance_criteria"]["bash_check"]["input_params"]["command"]
    # 补丁内联自包含（bash_check 在任务隔离环境执行，宿主绝对路径不可达）
    assert "git apply <<'SWE_TEST_PATCH'" in cmd
    assert "diff --git t" in cmd  # 补丁正文内联
    assert "python -m pytest t0 t1 -q" in cmd  # FAIL_TO_PASS 全量入 oracle
    assert "pip install -q pytest" in cmd  # 判定环境缺 pytest 时自装
    assert case["external_bundle"]["fail_to_pass"] == ["t0", "t1"]
    assert case["external_bundle"]["base_commit"] == "abc1234"


def test_oracle_resets_test_files_before_applying_patch():
    """好 agent 会自己写同名测试 → patch 会冲突；oracle 先把 patch 触及的
    测试文件重置到基线再应用（SWE-bench 官方口径：干净测试文件）。"""
    row = _swe_row("django__django-16377", n_f2p=1)
    row["test_patch"] = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1,2 @@\n+a\n"
        "diff --git a/tests/test_y.py b/tests/test_y.py\n"
        "--- a/tests/test_y.py\n+++ b/tests/test_y.py\n@@ -1 +1,2 @@\n+b\n")
    cmd = ext._oracle_command(row, "/mat/bundle", ["tests/test_x.py::t"])
    assert cmd.startswith("git checkout -- tests/test_x.py tests/test_y.py\n")
    assert "git apply <<'SWE_TEST_PATCH'" in cmd
    assert "python -m pytest tests/test_x.py::t -q" in cmd


def test_patch_test_files_extracts_b_side_paths_deduped():
    patch = ("diff --git a/keep.py b/keep.py\n--- a/keep.py\n"
             "diff --git a/dup.py b/dup.py\n--- a/dup.py\n"
             "diff --git a/dup.py b/dup.py\n--- a/dup.py\n")
    assert ext._patch_test_files(patch) == ["keep.py", "dup.py"]


def test_oracle_command_falls_back_to_bundle_path_for_oversized_patch():
    row = _swe_row("big__repo-1")
    row["test_patch"] = "x" * 20000  # 超 16KB 内联上限
    cmd = ext._oracle_command(row, "/mat/external/swe_bundles/big__repo-1", ["t1"])
    assert cmd.startswith("git apply '/mat/external/swe_bundles/big__repo-1/test.patch'")
    assert "SWE_TEST_PATCH" not in cmd


def test_bundle_prep_commands_clone_and_pin_commit():
    cmds = ext.bundle_prep_commands(_swe_row("x__y-1", repo="o/r"), "/mat")
    assert cmds[0] == 'git clone https://github.com/o/r.git "/mat/external/swe_repos/x__y-1"'
    assert cmds[1] == 'git -C "/mat/external/swe_repos/x__y-1" checkout abc1234'


# ── 轨迹库记录与许可画像 ─────────────────────────────────────────────────
def test_swe_library_record_license_and_labels():
    rec = ext.to_swe_library_record(_swe_row(n_f2p=3))
    assert rec["license_profile"] == {"dataset": "MIT", "source_repo": "unverified"}
    assert rec["labels"] == {"fail_to_pass": 3, "pass_to_pass": 1}
    assert rec["difficulty_band"] == "easy"
    assert rec["sizes"]["problem_statement"] > 0 and rec["sizes"]["test_patch"] > 0


def test_trace_pool_record_maps_resolved_license_and_tools():
    rec_in = {
        "instance_id": "gluesql__gluesql-67", "repo": "gluesql/gluesql",
        "license": "Apache-2.0", "language": "rust",
        "trajectory_id": "tid-1",
        "messages": [{"role": "system", "content": "sys"},
                     {"role": "user", "content": "fix it " * 500},
                     {"role": "assistant", "content": "done"}],
        "tools": [json.dumps({"function": {"name": "execute_bash"}}),
                  "{bad json"],
        "resolved": -1,
        "metadata": {"category": "bug-fix",
                     "reference_patch": {"patch": "diff --git"}},
    }
    rec = ext.to_trace_pool_record(rec_in, "data/openhands/qwen36_27b/swe-rebench-v2/train-x.parquet")
    assert rec["source_ref"] == "gluesql__gluesql-67"
    assert rec["license_profile"] == {"dataset": "CC-BY-4.0", "source_repo": "Apache-2.0"}
    assert rec["resolved"] == -1 and rec["category"] == "bug-fix"
    assert rec["steps"] == 3
    assert rec["tool_names"] == ["execute_bash"]  # 坏 JSON 工具项跳过不炸
    assert rec["has_reference_patch"] is True
    assert len(rec["first_user_head"]) <= 1200  # 正文 bounded，全量按 shard 回放
    assert ext.scaffold_of_shard(rec["shard"]) == "openhands"


def test_trace_pool_record_tolerates_missing_fields():
    rec = ext.to_trace_pool_record({"trajectory_id": "t", "resolved": 1}, "data/sweagent/m/d/f.parquet")
    assert rec["steps"] == 0 and rec["tool_names"] == []
    assert rec["license_profile"]["source_repo"] == "unverified"
    assert rec["first_user_head"] == ""


# ── oracle 计数容错（非列表/坏 JSON → 0） ────────────────────────────────
def test_oracle_counts_survive_bad_payloads():
    """FAIL_TO_PASS / PASS_TO_PASS 载荷损坏 → 计 0 不抛（fail-closed 口径）。"""
    bad_payloads = [
        "not-json{",            # 坏 JSON → ValueError → 0
        json.dumps("a-string"),  # 合法 JSON 但非列表 → 0
        json.dumps({"k": 1}),    # 字典同理 → 0
        None,                    # 键缺失（raw=None 非字符串）→ 0
    ]
    for raw in bad_payloads:
        row = _swe_row()
        row["FAIL_TO_PASS"] = raw
        row["PASS_TO_PASS"] = raw
        assert ext._fail_to_pass_count(row) == 0, raw
        assert ext._pass_to_pass_count(row) == 0, raw

    # 性质对照：合法列表计数 = 元素数（防"恒 0"假绿）
    row = _swe_row()
    row["FAIL_TO_PASS"] = json.dumps(["t0", "t1", "t2"])
    assert ext._fail_to_pass_count(row) == 3
