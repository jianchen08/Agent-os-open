#!/usr/bin/env python3
"""装机基线漂移巡检（ADR 2026-11-07-plugin-tiered-single-copy-model 刀 4）。

输入装机 `<user_root>/plugin-baseline.json` + 包内种子树（resources 根），
对账基线 ↔ 种子漂移，供装机监控轮等外部轮询方调用（本脚本只读不写，不挂
CI——巡检对象在装机实例，由调度方轮询消费）：

  - 基线落后种子（种子版本更新）= 漂移告警（warn）——装机该升级/重装；
  - 基线超前种子（基线比包新）= 漂移告警（warn）——监控对错了 pack-root
    或包被回退，同为「基线 vs 包版本漂移」（ADR §2.3）；
  - 单侧缺失（基线有条目种子无 / 种子有插件基线无）= 漂移告警（warn）——
    基线未随包翻新或包面残缺；
  - 基线损坏（文件缺失/非 JSON/schema≠1/plugins 结构不符/条目缺 version）
    = error（钉死格式见 ADR §2.1，装不出的基线属安装器异常面）。

基线格式（ADR 钉死）：
  {"schema":1, "package_version":"", "written_at":"",
   "plugins":{"<id>":{"tier":2, "version":"", "files":{}}}}

用法（装机监控轮示例）：
  python scripts/check_plugin_baseline_drift.py \
      --baseline "$env:APPDATA\\agentos\\plugin-baseline.json" \
      --pack-root "<安装位>\\resources"
退出码：0 = 基线与种子一致；1 = 漂移告警（warn，轮询方按告警面处理）；
2 = error（基线损坏/种子树缺失/清单不可读，需升级处置）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_package_plugin_freshness import read_seed_versions  # noqa: E402 —— 种子读取单源复用

_SEED_REL = Path("plugins") / "shared"


class BaselineError(RuntimeError):
    """基线损坏（钉死格式不符）——error 面（exit 2）。"""


def load_baseline(path: Path) -> tuple[dict[str, str], str]:
    """读基线 → ({plugin_id: version}, package_version)；损坏抛 BaselineError。"""
    if not path.is_file():
        raise BaselineError(f"基线文件不存在：{path}（安装器未写 = 异常面，非「用户没装插件」）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"基线不可读/非 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise BaselineError("基线结构不符：顶层须为对象")
    if data.get("schema") != 1:
        raise BaselineError(f"基线 schema 非钉死格式（须为 1）：{data.get('schema')!r}")
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        raise BaselineError("基线结构不符：缺 plugins 对象")
    versions: dict[str, str] = {}
    for pid, entry in plugins.items():
        ver = entry.get("version") if isinstance(entry, dict) else None
        if not isinstance(ver, str) or not ver:
            raise BaselineError(f"基线条目损坏：plugins.{pid} 缺 version 字符串")
        versions[str(pid)] = ver
    package_version = data.get("package_version")
    if not isinstance(package_version, str):
        package_version = ""
    return versions, package_version


def _version_key(ver: str) -> tuple[int, ...] | None:
    """点分整数版本 → 可序元组；非点分整数形态返回 None（方向标「不等」）。"""
    parts = ver.split(".")
    if parts and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return None


def _strip_zeros(key: tuple[int, ...]) -> tuple[int, ...]:
    """尾零归一：(1, 0) == (1, 0, 0)——数值同形异写按「不等」处理不误标方向。"""
    while len(key) > 1 and key[-1] == 0:
        key = key[:-1]
    return key


def compare_baseline(baseline: dict[str, str], seed: dict[str, str]) -> list[str]:
    """基线 ↔ 种子漂移明细（纯函数，可测；空=一致，非空=warn 面）。"""
    lines: list[str] = []
    for pid in sorted(set(baseline) & set(seed)):
        b_ver, s_ver = baseline[pid], seed[pid]
        if b_ver == s_ver:
            continue
        b_key, s_key = _version_key(b_ver), _version_key(s_ver)
        behind = b_key is not None and s_key is not None and _strip_zeros(b_key) < _strip_zeros(s_key)
        ahead = b_key is not None and s_key is not None and _strip_zeros(b_key) > _strip_zeros(s_key)
        if behind:
            lines.append(f"落后 {pid}：基线 {b_ver} < 种子 {s_ver}")
        elif ahead:
            lines.append(f"超前 {pid}：基线 {b_ver} > 种子 {s_ver}（监控对错包或包被回退？）")
        else:
            lines.append(f"不等 {pid}：基线 {b_ver} / 种子 {s_ver}")
    for pid in sorted(set(baseline) - set(seed)):
        lines.append(f"种子缺条目 {pid}：基线 {baseline[pid]}，包内种子树无此插件")
    for pid in sorted(set(seed) - set(baseline)):
        lines.append(f"基线缺条目 {pid}：种子 {seed[pid]}，基线无此插件（安装器未写全？）")
    return lines


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="装机基线漂移巡检（plugin-baseline.json ↔ 包内种子树，只读；ADR 2026-11-07 刀 4）"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="装机基线路径（ADR 惯例 <user_root>/plugin-baseline.json，装机即 APPDATA 下 agentos 目录）",
    )
    parser.add_argument(
        "--pack-root", type=Path, required=True, help="包内种子树所在根（含 plugins/shared 子树，装机即 <安装位>\\resources）"
    )
    args = parser.parse_args(argv)

    try:
        baseline, package_version = load_baseline(args.baseline.resolve())
    except BaselineError as exc:
        print(f"[baseline-drift] ERROR {exc}")
        return 2

    seed_dir = args.pack_root.resolve() / _SEED_REL
    if not seed_dir.is_dir():
        print(f"[baseline-drift] ERROR 环境不可判：种子树不存在：{seed_dir}（--pack-root 传错？）")
        return 2
    try:
        seed, seed_errors = read_seed_versions(seed_dir)
    except OSError as exc:
        print(f"[baseline-drift] ERROR 环境不可判：种子树不可读：{exc}")
        return 2
    if seed_errors:
        print(f"[baseline-drift] ERROR 种子树清单缺陷（{len(seed_errors)} 项）：")
        for line in seed_errors:
            print(f"[baseline-drift]   - {line}")
        return 2

    drift = compare_baseline(baseline, seed)
    print(
        f"[baseline-drift] 对账面：基线 {len(baseline)} 插件（package_version={package_version or '未知'}）"
        f" ↔ 种子 {len(seed)} 插件"
    )
    if drift:
        print(f"[baseline-drift] WARN 漂移告警（{len(drift)} 项）：")
        for line in drift:
            print(f"[baseline-drift]   - {line}")
        print("[baseline-drift] 动作：基线落后 → 装机升级/重装即基线翻新；超前/缺失 → 核对")
        print("       --pack-root 与安装包版本（ADR 2026-11-07 §2.1 基线对账）")
        return 1
    print("[baseline-drift] PASS 基线与种子一致（无漂移）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
