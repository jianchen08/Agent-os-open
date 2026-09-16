# -*- coding: utf-8 -*-
"""外部数据集拉取脚本（运维通道）：HF 数据集 → 判定种子套件 + 外部轨迹样本库。

用法（root venv，项目根 cwd）：
  .venv/Scripts/python.exe plugins/shared/system/eval_harness/fetch_external_trajectories.py [--dry-run]

网络：huggingface.co 不可达时走 hf-mirror.com（HF_ENDPOINT 缺省已设，可覆盖）。
产物（ADR 2026-09-16-external-dataset-sourcing）：
  config/self_evolve/suites/external/swe_verified_seed.yaml    判定种子套件（15 题）
  reports/eval/trajectory_library/external_swe_verified_*.json 外部任务库（全量 500）
  reports/eval/trajectory_library/external_open_swe_traces_*.json 轨迹样本池
  {物料区}/external/swe_bundles/{iid}/test.patch + meta.json    bundle（仓库外）
  {物料区}/external/prep_swe_repos.sh                           种子仓库预置脚本
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)

import external_datasets as ext  # noqa: E402

_SUITES_OUT = os.path.join(ROOT, "config", "self_evolve", "suites", "external")
_LIBRARY_OUT = os.path.join(ROOT, "reports", "eval", "trajectory_library")


def default_materials_root() -> str:
    env = os.environ.get("AGENTOS_EVAL_MATERIALS_DIR", "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(ROOT.rstrip("/\\")), "agentos_selfevolve")


def _load_parquet_rows(path: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    cols = {c: table.column(c).to_pylist() for c in table.column_names}
    n = table.num_rows
    return [{k: v[i] for k, v in cols.items()} for i in range(n)]


def fetch_swe_verified(materials_root: str, seed_per_band: int,
                       max_issue_chars: int, dry: bool) -> dict[str, Any]:
    from huggingface_hub import hf_hub_download
    src = ext.SOURCES["swe_bench_verified"]
    path = hf_hub_download(src["repo_id"], src["path"], repo_type="dataset")
    rows = _load_parquet_rows(path)
    library_records = [ext.to_swe_library_record(r) for r in rows]
    seed = ext.select_seed(rows, per_band=seed_per_band,
                           max_issue_chars=max_issue_chars)
    seed_rows = [r for band in ext.BAND_ORDER for r in seed[band]]
    cases = [ext.adapt_seed_case(r, materials_root) for r in seed_rows]
    suite = {
        "name": "external-swe-verified-seed",
        "mode": "coding",
        "settle_timeout_seconds": 3600,
        "source_note": (
            f"外部数据集种子（SWE-bench Verified，MIT；来源仓库许可未逐仓核验）。"
            f"种子选取：difficulty 三档各 {seed_per_band} 题（仓库分散抽样，"
            f"题面>{max_issue_chars} 字符不入种子）。执行前置：先跑 "
            f"{materials_root}/external/prep_swe_repos.sh 预置仓库。"),
        "cases": cases,
    }
    if not dry:
        for r in seed_rows:
            bundle_dir = os.path.join(materials_root, "external", "swe_bundles",
                                      str(r["instance_id"]))
            os.makedirs(bundle_dir, exist_ok=True)
            with open(os.path.join(bundle_dir, "test.patch"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(str(r.get("test_patch") or ""))
            meta = {"instance_id": r.get("instance_id"), "repo": r.get("repo"),
                    "base_commit": r.get("base_commit"),
                    "environment_setup_commit": r.get("environment_setup_commit"),
                    "difficulty": r.get("difficulty"),
                    "fail_to_pass": json.loads(r["FAIL_TO_PASS"])
                    if isinstance(r.get("FAIL_TO_PASS"), str) else []}
            with open(os.path.join(bundle_dir, "meta.json"), "w",
                      encoding="utf-8", newline="\n") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=1)
        prep_lines = ["#!/usr/bin/env bash", "# SWE-bench 种子仓库预置（评测执行前跑一次）",
                      "set -e"]
        for r in seed_rows:
            prep_lines += ext.bundle_prep_commands(r, materials_root)
        prep_path = os.path.join(materials_root, "external", "prep_swe_repos.sh")
        os.makedirs(os.path.dirname(prep_path), exist_ok=True)
        with open(prep_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(prep_lines) + "\n")
    return {"suite": suite, "library_records": library_records,
            "seed_counts": {b: len(v) for b, v in seed.items()}}


def fetch_open_swe_traces(pool_per_shard: int, dry: bool) -> dict[str, Any]:
    from huggingface_hub import hf_hub_download
    src = ext.SOURCES["open_swe_traces"]
    records = []
    for shard in src["sample_shards"]:
        path = hf_hub_download(src["repo_id"], shard, repo_type="dataset")
        rows = _load_parquet_rows(path)
        step = max(1, len(rows) // max(1, pool_per_shard))
        sample = rows[::step][:pool_per_shard]
        records += [ext.to_trace_pool_record(r, shard) for r in sample]
    return {"records": records,
            "shards": list(src["sample_shards"]),
            "pool_per_shard": pool_per_shard}


def _write_library(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="all",
                        choices=["all", "swe_verified", "open_swe_traces"])
    parser.add_argument("--seed-per-band", type=int, default=5)
    parser.add_argument("--max-issue-chars", type=int, default=1500)
    parser.add_argument("--pool-per-shard", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    tag = time.strftime("%Y%m%d_%H%M%S")
    materials_root = default_materials_root().replace("\\", "/")

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    written = []
    if args.source in ("all", "swe_verified"):
        result = fetch_swe_verified(materials_root, args.seed_per_band,
                                    args.max_issue_chars, args.dry_run)
        print("SWE-bench Verified: 全量", len(result["library_records"]),
              "任务；种子分带:", result["seed_counts"])
        suite_path = os.path.join(_SUITES_OUT, "swe_verified_seed.yaml")
        if not args.dry_run:
            os.makedirs(_SUITES_OUT, exist_ok=True)
            body = yaml.safe_dump(result["suite"], allow_unicode=True,
                                  sort_keys=False, width=1000)
            header = (f"# 外部数据集种子套件（SWE-bench Verified，MIT）——"
                      f"脚本生成物，勿手改\n# 生成: {tag} | "
                      f"溯源与许可画像见 reports/eval/trajectory_library/\n")
            with open(suite_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(header + body)
            _write_library(os.path.join(
                _LIBRARY_OUT, f"external_swe_verified_{tag}.json"), {
                "library_version": "external-1.0",
                "source": "princeton-nlp/SWE-bench_Verified",
                "generated_tag": tag,
                "records": result["library_records"]})
            written += [suite_path]
        else:
            print("(dry-run: 套件/库不落盘)")
    if args.source in ("all", "open_swe_traces"):
        pool = fetch_open_swe_traces(args.pool_per_shard, args.dry_run)
        print("Open-SWE-Traces 池提取:", len(pool["records"]), "条轨迹记录")
        if not args.dry_run:
            pool_path = os.path.join(
                _LIBRARY_OUT, f"external_open_swe_traces_{tag}.json")
            _write_library(pool_path, {
                "library_version": "external-1.0",
                "source": "nvidia/Open-SWE-Traces",
                "generated_tag": tag,
                "shards": pool["shards"],
                "records": pool["records"]})
            written.append(pool_path)
    for path in written:
        print("写出:", path)


if __name__ == "__main__":
    main()
