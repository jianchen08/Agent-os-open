# -*- coding: utf-8 -*-
"""外部公开数据集适配（纯函数核心）：数据集记录 → 判定题集 case + 轨迹样本库记录。

来源路线（ADR 2026-09-16-external-dataset-sourcing，调研报告 P0 复用清单）：
- SWE-bench Verified（500 任务，FAIL_TO_PASS/PASS_TO_PASS oracle，MIT）→
  coding 域判定种子：difficulty 分带 + 仓库分散的分层抽样，任务改造为本
  系统工具面可完成形态（仓库 worktree + issue 修复 + bash_check 测试车道）；
- Open-SWE-Traces（20 万+轨迹，resolved 标签，CC BY 4.0 + per-repo 许可）→
  轨迹样本池：多 scaffold 轨迹记录入轨迹库供归因/共识参照，不改造成派发
  case（他方 scaffold 轨迹绑定其工具面，重放即伪评测）。

本模块只做纯映射（dict in / dict out），下载与落盘见
fetch_external_trajectories.py（运维通道）。
"""
from __future__ import annotations

import json
from typing import Any

# 数据集来源登记（许可画像：dataset 许可与来源仓库许可分别记录——调研报告
# 红线：不能用单一 dataset license 覆盖衍生用途）
SOURCES: dict[str, dict[str, Any]] = {
    "swe_bench_verified": {
        "repo_id": "princeton-nlp/SWE-bench_Verified",
        "path": "data/test-00000-of-00001.parquet",
        "dataset_license": "MIT",
        "domain": "coding",
        "source_repo_license": "unverified",  # issue 来源仓库许可混合，逐仓核验归执行期
    },
    "open_swe_traces": {
        "repo_id": "nvidia/Open-SWE-Traces",
        "dataset_license": "CC-BY-4.0",
        "domain": "coding",
        "source_repo_license_field": "license",  # OST 每条记录自带来源仓库许可
        # 样本池 shard：三 scaffold（sweagent/openhands/minisweagent）各一
        "sample_shards": [
            "data/sweagent/qwen35_122b/swe-rebench-v2/train-00008-of-00009.parquet",
            "data/openhands/qwen36_27b/swe-rebench-v2/train-00011-of-00012.parquet",
            "data/minisweagent/qwen36_27b/swe-rebench-v2/train-00021-of-00022.parquet",
        ],
    },
}

# SWE-bench Verified difficulty 字段 → 三档带（>4h 样本仅 3 条，并入 hard）
DIFFICULTY_BANDS = {
    "<15 min fix": "easy",
    "15 min - 1 hour": "medium",
    "1-4 hours": "hard",
    ">4 hours": "hard",
}
BAND_ORDER = ("easy", "medium", "hard")


def difficulty_band(label: Any) -> str:
    return DIFFICULTY_BANDS.get(str(label or "").strip(), "")


def _fail_to_pass_count(row: dict[str, Any]) -> int:
    raw = row.get("FAIL_TO_PASS")
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return len(v) if isinstance(v, list) else 0
    except ValueError:
        return 0


def select_seed(rows: list[dict[str, Any]], per_band: int = 5,
                max_issue_chars: int = 1500) -> dict[str, list[dict[str, Any]]]:
    """分层种子选取：按难度带分组，带内按仓库轮转各取一题（防仓库集中），
    题面超长（截断会歪曲题目）与分带未知的不入种子。确定性排序。
    返回 {band: [row, ...]}。
    """
    by_band: dict[str, list[dict[str, Any]]] = {b: [] for b in BAND_ORDER}
    candidates: dict[str, list[dict[str, Any]]] = {b: [] for b in BAND_ORDER}
    for row in rows:
        band = difficulty_band(row.get("difficulty"))
        if band == "":
            continue
        if len(str(row.get("problem_statement") or "")) > max_issue_chars:
            continue
        candidates[band].append(row)
    for band, pool in candidates.items():
        pool.sort(key=lambda r: (str(r.get("repo") or ""), str(r.get("instance_id") or "")))
        repos: dict[str, list[dict[str, Any]]] = {}
        for row in pool:
            repos.setdefault(str(row.get("repo") or ""), []).append(row)
        queues = [repos[k] for k in sorted(repos)]
        picked: list[dict[str, Any]] = []
        # 仓库队列轮转，每轮各取一题——分散优先，同仓自然补齐配额
        while len(picked) < per_band and any(queues):
            for queue in queues:
                if queue and len(picked) < per_band:
                    picked.append(queue.pop(0))
        by_band[band] = picked
    return by_band


_INSTRUCTION_TEMPLATE = """你在一个真实开源仓库的 git 工作副本中工作（{repo}@{base_commit}）。下面是一个真实的 issue，请在仓库中定位并修复它。

--- 8< --- issue 原文 --- 8< ---
{problem_statement}
--- >8 --- issue 结束 --- >8 ---

约束与提示：
- 只修改必要的源码；不得修改或绕过测试本身；
- 环境缺依赖时先自行安装（如 pip install -e . 以及运行测试所需的依赖）；
- 完成后确认以下测试可通过：{tests}。"""


def _oracle_command(row: dict[str, Any], bundle_dir: str,
                    tests: list[str]) -> str:
    """bash_check 判定命令。bash_check 经任务面 bash 工具在任务隔离环境的
    workspace cwd 执行——bundle 的宿主绝对路径在该环境不可达，故补丁内联
    heredoc 自包含（当前种子补丁均 <1KB）；超长补丁回退 bundle 文件路径
    （该形态要求宿主路径可见，执行期验证）。"""
    patch = str(row.get("test_patch") or "")
    pytest_line = f"python -m pytest {' '.join(tests)} -q"
    if 0 < len(patch) <= 16384:
        return (f"git apply <<'SWE_TEST_PATCH'\n{patch}\nSWE_TEST_PATCH\n"
                f"python -m pytest --version >/dev/null 2>&1 || pip install -q pytest\n"
                f"{pytest_line}")
    return f"git apply '{bundle_dir}/test.patch' && {pytest_line}"


def adapt_seed_case(row: dict[str, Any], materials_root: str) -> dict[str, Any]:
    """SWE-bench 任务 → 本系统派发 case（仓库 worktree + bash_check 测试车道）。"""
    iid = str(row["instance_id"])
    bundle_dir = f"{materials_root}/external/swe_bundles/{iid}".replace("\\", "/")
    repo_dir = f"{materials_root}/external/swe_repos/{iid}".replace("\\", "/")
    tests = json.loads(row["FAIL_TO_PASS"]) if isinstance(row.get("FAIL_TO_PASS"), str) \
        else list(row.get("FAIL_TO_PASS") or [])
    command = _oracle_command(row, bundle_dir, tests)
    return {
        "id": f"swev_{iid}",
        "source": "swe_bench_verified",
        "category": difficulty_band(row.get("difficulty")),
        "target": "general_agent",
        "anchor": "repo",
        "workspace": repo_dir,
        "workspace_mode": "worktree",
        "messages": [_INSTRUCTION_TEMPLATE.format(
            repo=row.get("repo"), base_commit=row.get("base_commit"),
            problem_statement=row.get("problem_statement"),
            tests=", ".join(tests))],
        "acceptance_criteria": {
            "bash_check": {"input_params": {"command": command}},
        },
        "external_bundle": {
            "dataset": SOURCES["swe_bench_verified"]["repo_id"],
            "repo": row.get("repo"),
            "base_commit": row.get("base_commit"),
            "environment_setup_commit": row.get("environment_setup_commit"),
            "test_patch_file": f"{bundle_dir}/test.patch",
            "fail_to_pass": tests,
        },
    }


def to_swe_library_record(row: dict[str, Any]) -> dict[str, Any]:
    """SWE-bench 任务 → 外部轨迹库紧凑记录（含许可画像与 oracle 标签）。"""
    src = SOURCES["swe_bench_verified"]
    return {
        "source_ref": str(row.get("instance_id") or ""),
        "source": src["repo_id"],
        "domain": src["domain"],
        "difficulty_band": difficulty_band(row.get("difficulty")),
        "repo": row.get("repo"),
        "base_commit": row.get("base_commit"),
        "created_at": row.get("created_at"),
        "version": row.get("version"),
        "license_profile": {
            "dataset": src["dataset_license"],
            "source_repo": src["source_repo_license"],
        },
        "labels": {
            "fail_to_pass": _fail_to_pass_count(row),
            "pass_to_pass": _pass_to_pass_count(row),
        },
        "sizes": {
            "problem_statement": len(str(row.get("problem_statement") or "")),
            "patch": len(str(row.get("patch") or "")),
            "test_patch": len(str(row.get("test_patch") or "")),
        },
    }


def _pass_to_pass_count(row: dict[str, Any]) -> int:
    raw = row.get("PASS_TO_PASS")
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return len(v) if isinstance(v, list) else 0
    except ValueError:
        return 0


def _tool_names(tools: Any) -> list[str]:
    names = []
    for t in tools or []:
        try:
            spec = json.loads(t) if isinstance(t, str) else t
            names.append(str(spec["function"]["name"]))
        except (ValueError, KeyError, TypeError):
            continue
    return names


def to_trace_pool_record(rec: dict[str, Any], shard: str,
                         max_head_chars: int = 1200) -> dict[str, Any]:
    """Open-SWE-Traces 轨迹记录 → 轨迹样本池记录（溯源可回放，正文 bounded）。"""
    src = SOURCES["open_swe_traces"]
    messages = rec.get("messages") or []
    first_user = next((m.get("content") for m in messages
                       if m.get("role") == "user"), "")
    last_msg = str(messages[-1].get("content") if messages else "")
    metadata = rec.get("metadata") or {}
    return {
        "source": src["repo_id"],
        "shard": shard,
        "trajectory_id": str(rec.get("trajectory_id") or ""),
        "source_ref": str(rec.get("instance_id") or ""),
        "repo": rec.get("repo"),
        "language": rec.get("language"),
        "license_profile": {
            "dataset": src["dataset_license"],
            "source_repo": rec.get("license") or "unverified",
        },
        "resolved": rec.get("resolved"),
        "category": metadata.get("category"),
        "steps": len(messages),
        "tool_names": _tool_names(rec.get("tools")),
        "has_reference_patch": bool((metadata.get("reference_patch") or {}).get("patch")
                                    if isinstance(metadata.get("reference_patch"), dict)
                                    else metadata.get("reference_patch")),
        "first_user_head": str(first_user or "")[:max_head_chars],
        "last_message_head": last_msg[:max_head_chars],
    }


def scaffold_of_shard(shard: str) -> str:
    """data/<scaffold>/<model>/<dataset>/<file> → scaffold 名。"""
    parts = str(shard or "").split("/")
    return parts[1] if len(parts) > 2 else ""


def bundle_prep_commands(row: dict[str, Any], materials_root: str) -> list[str]:
    """种子仓库预置命令（评测执行期由运维跑一次；clone 通道按执行期网络打通）。"""
    iid = str(row["instance_id"])
    repo_dir = f"{materials_root}/external/swe_repos/{iid}".replace("\\", "/")
    return [
        f"git clone https://github.com/{row['repo']}.git \"{repo_dir}\"",
        f"git -C \"{repo_dir}\" checkout {row['base_commit']}",
    ]
