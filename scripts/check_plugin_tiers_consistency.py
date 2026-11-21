#!/usr/bin/env python3
"""插件分级清单一致性对账闸（ADR 2026-11-07-plugin-tiered-single-copy-model 刀 4）。

Tier1 清单 = config/kernel/plugin_tiers.yaml（单一真源，本闸只读不写）。
清单外条目全部默认 Tier2（无显式 Tier2 列表），故只对账两面：

  ①清单 → 磁盘：每个 Tier1 条目必须存在——
      - safety_plugins（插件目录类）：plugins/shared/<id>/ 与
        plugins/shared/<id>/plugin.json 必须存在；
      - shared_modules 目录条目（_host/_libs/_root_api 等共享根）：目录必须
        存在（共享根非插件，无 plugin.json 属预期，不要求）；
      - shared_modules 单文件条目（`.py` 结尾）：plugins/shared/<name> 文件
        必须存在。
  ②磁盘 → 清单：plugins/shared 根层全部 `_` 前缀条目（目录与单文件）必须
      登记在清单内——新增共享件忘登记 = 红（ADR §2.1：共享件归 Tier 1 的
      理由②③要求登记面完整，漏登记即出现无归属的共享副本）。

扫描面 = git ls-files（跟踪条目）：__pycache__/_dbg_tmp_neighbor 等未跟踪
运行时产物不进对账面，闸对干净检出与开发机同一判定（CI/本地零分叉）。

接线：run_gates 注册 id=plugin-tiers（domain=plugins），ci.yml 恒跑 job
（traceability-gate，无 if:）承载——plugin_tiers.yaml 属 config/ 面，纯
config 变更不点亮 python_full 车道（scripts/ci_changed_areas.py），恒跑
job 是「清单变更必过闸」的唯一落点（ci.yml 注释同源）。

用法：
  python scripts/check_plugin_tiers_consistency.py                # 对账本仓
  python scripts/check_plugin_tiers_consistency.py --repo <dir>   # 指定仓根
退出码：0 = 一致；1 = 不一致（打印逐条差异明细）；2 = 环境不可判（非 git
仓 / git 不可用 / 共享根缺失）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_DEFAULT = Path(__file__).resolve().parents[1]
_MANIFEST_REL = Path("config") / "kernel" / "plugin_tiers.yaml"
_SHARED_REL = Path("plugins") / "shared"

#: 清单分桶键 → 语义（load_manifest 契约，测试直接消费）。
BUCKET_KEYS = ("plugin_dirs", "module_dirs", "module_files")


class ManifestError(RuntimeError):
    """清单缺失/不可解析/结构不符——校验面自身损坏，一律红（exit 1）。"""


class GitUnavailableError(RuntimeError):
    """git 机制不可用（非 git 仓/命令失败）——环境不可判（exit 2）。"""


def load_manifest(path: Path) -> dict[str, list[str]]:
    """读 Tier1 清单并按类分桶。

    分类规则（清单语义，见 plugin_tiers.yaml 头注）：safety_plugins = 安全
    关键插件目录；shared_modules = 共享件（`.py` 结尾为单文件模块，其余为
    共享目录根）。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"清单不可读：{exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ManifestError(f"清单 YAML 不可解析：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tier1"), dict):
        raise ManifestError("清单结构不符：缺 tier1 映射")
    tier1 = data["tier1"]
    safety = tier1.get("safety_plugins", [])
    shared = tier1.get("shared_modules", [])
    if not isinstance(safety, list) or not isinstance(shared, list):
        raise ManifestError("清单结构不符：tier1.safety_plugins / tier1.shared_modules 须为列表")
    buckets: dict[str, list[str]] = {key: [] for key in BUCKET_KEYS}
    for item in safety:
        if not isinstance(item, str) or not item:
            raise ManifestError(f"清单结构不符：safety_plugins 含非法条目：{item!r}")
        buckets["plugin_dirs"].append(item)
    for item in shared:
        if not isinstance(item, str) or not item:
            raise ManifestError(f"清单结构不符：shared_modules 含非法条目：{item!r}")
        if item.endswith(".py"):
            buckets["module_files"].append(item)
        else:
            buckets["module_dirs"].append(item)
    return buckets


def gather_tracked_entries(repo: Path, root_rel: Path) -> set[str]:
    """root 下 git 跟踪条目的根层名字集合（目录与单文件混排，ls-files 折叠）。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", root_rel.as_posix()],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitUnavailableError(f"git ls-files 失败：{detail}")
    prefix = root_rel.as_posix() + "/"
    names: set[str] = set()
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode("utf-8", "replace")
        if not rel.startswith(prefix):
            continue
        name = rel[len(prefix) :].split("/", 1)[0]
        if name:
            names.add(name)
    return names


def _manifest_id_map(shared_disk: Path, tracked: set[str]) -> tuple[dict[str, str], list[str]]:
    """扫描跟踪面插件目录读 manifest id → (id→目录名, 问题明细)。

    safety_plugins 条目语义 = **manifest id**（plugin.json 的 id，非目录名——内核
    启动守卫按 id 匹配）。id 跨目录重复 = 配置错误（守卫同样拒启）。
    """
    ids: dict[str, str] = {}
    problems: list[str] = []
    import json
    for name in sorted(tracked):
        mf = shared_disk / name / "plugin.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"plugin.json 不可解析：plugins/shared/{name}/plugin.json：{exc}")
            continue
        pid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(pid, str) or not pid:
            problems.append(f"plugin.json 缺 id：plugins/shared/{name}/plugin.json")
            continue
        if pid in ids:
            problems.append(f"manifest id 重复（跨目录同名 id）：{pid} ∈ {{{ids[pid]}, {name}}}")
            continue
        ids[pid] = name
    return ids, problems


def check_consistency(buckets: dict[str, list[str]], tracked: set[str], shared_disk: Path) -> list[str]:
    """两面对账（纯函数，可测）：清单→磁盘 + 磁盘→清单。返回问题明细（空=一致）。"""
    problems: list[str] = []
    registered: set[str] = set()
    id_map, id_problems = _manifest_id_map(shared_disk, tracked)
    problems.extend(id_problems)
    for pid in buckets["plugin_dirs"]:
        registered.add(pid)
        resolved = id_map.get(pid)
        if resolved is None:
            problems.append(f"清单条目悬空（manifest id 无对应目录）：{pid}")
        else:
            registered.add(id_map[pid])
    for mid in buckets["module_dirs"]:
        registered.add(mid)
        if mid not in tracked:
            problems.append(f"清单条目悬空（plugins/shared 下无跟踪条目）：{mid}")
        elif not (shared_disk / mid).is_dir():
            problems.append(f"共享目录不在磁盘：plugins/shared/{mid}")
    for fname in buckets["module_files"]:
        registered.add(fname)
        if fname not in tracked:
            problems.append(f"清单条目悬空（plugins/shared 下无跟踪条目）：{fname}")
        elif not (shared_disk / fname).is_file():
            problems.append(f"共享单文件不在磁盘：plugins/shared/{fname}")
    for name in sorted(tracked):
        if name.startswith("_") and name not in registered:
            problems.append(f"未登记共享件（`_` 前缀须入清单 Tier1）：plugins/shared/{name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="插件分级清单一致性对账闸（Tier1 清单 ↔ plugins/shared 两面，ADR 2026-11-07 刀 4）"
    )
    parser.add_argument("--repo", type=Path, default=_REPO_DEFAULT, help="仓库根（默认脚本所在仓）")
    parser.add_argument("--manifest", type=Path, default=None, help="Tier1 清单路径（默认 <repo>/config/kernel/plugin_tiers.yaml）")
    parser.add_argument("--shared-root", type=Path, default=None, help="共享根（默认 <repo>/plugins/shared）")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    shared = (args.shared_root or repo / _SHARED_REL).resolve()
    manifest = (args.manifest or repo / _MANIFEST_REL).resolve()
    try:
        root_rel = shared.relative_to(repo)
    except ValueError:
        print(f"[tiers-consistency] FAIL 环境不可判：共享根不在仓库内：{shared}（repo={repo}）")
        return 2

    try:
        buckets = load_manifest(manifest)
    except ManifestError as exc:
        print(f"[tiers-consistency] FAIL {exc}")
        return 1
    if not shared.is_dir():
        print(f"[tiers-consistency] FAIL 环境不可判：共享根不存在：{shared}")
        return 2
    try:
        tracked = gather_tracked_entries(repo, root_rel)
    except GitUnavailableError as exc:
        print(f"[tiers-consistency] FAIL 环境不可判：{exc}")
        return 2

    problems = check_consistency(buckets, tracked, shared)
    total = sum(len(buckets[key]) for key in BUCKET_KEYS)
    print(
        f"[tiers-consistency] 清单 {manifest.relative_to(repo).as_posix()}：{total} 条 Tier1 条目"
        f"（安全插件 {len(buckets['plugin_dirs'])} / 共享目录 {len(buckets['module_dirs'])}"
        f" / 共享单文件 {len(buckets['module_files'])}）；跟踪条目 {len(tracked)} 个"
    )
    if problems:
        print(f"[tiers-consistency] FAIL 两面不一致（{len(problems)} 项）：")
        for line in problems:
            print(f"[tiers-consistency]   - {line}")
        print("[tiers-consistency] 动作：清单增删条目须与 plugins/shared 实际同刀对齐")
        print("       （维护纪律见 config/kernel/plugin_tiers.yaml 头注；ADR")
        print("       2026-11-07-plugin-tiered-single-copy-model §2.1）")
        return 1
    print("[tiers-consistency] PASS 清单与实际一致（Tier1 条目齐全在位，无未登记 `_` 前缀共享件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
