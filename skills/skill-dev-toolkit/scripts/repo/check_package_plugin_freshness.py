#!/usr/bin/env python3
"""包内插件种子新鲜度闸（ADR 2026-11-07-plugin-tiered-single-copy-model 刀 4）。

契约：包内 Tier2 种子版本 == 仓内 HEAD（ADR §2.3「CI 对账闸」）——陈旧包
打不出来。打包链 electron-builder 经 extraResources 把 `plugins/shared`
整树照搬进安装包 resources（package.json），本闸把「照搬源 ↔ 仓内指定
提交」逐插件版本对账 fail-closed 化，消灭人肉 diff 兜底：

  输入 --pack-root（含 `plugins/shared` 子树的打包产物根——仓 worktree 或
  已装 resources 根皆可）逐插件读 plugin.json version；--repo + --ref
  （默认 HEAD）经 git ls-tree/show 读同路径 version；不等/单侧缺失/清单
  不可读 = 红并打印差异表。

插件面判定：子目录含 plugin.json 才算插件——`_host/_libs` 等共享根目录
与 `*.py` 单文件共享件天然不进对账面（与清单闸 plugins/shared 语义一致）。

接线：deploy/release_all.sh desktop 段链首（worktree@BUILD_HEAD 就位后、
cargo/NSIS 烘烤前，与 check_ci_gate 同为 fail-closed 链首取证）。选择
worktree 而非主仓工作树做 pack-root 是 release_all 既有语义（prep 段
「只烘已提交码，WIP 免疫」），主仓脏树不拦发布、种子树漂移必拦。

只读对账：不写任何文件。

用法：
  python scripts/check_package_plugin_freshness.py --pack-root <resources 根> [--repo <仓根>] [--ref HEAD]
退出码：0 = 全部一致；1 = 有差异（差异表见输出）；2 = 环境不可判（pack-root
缺失 / git 失败 / ref 不可解析）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_DEFAULT = Path(__file__).resolve().parents[1]
_SEED_REL = Path("plugins") / "shared"


class RefReadError(RuntimeError):
    """git 侧读取失败（命令错误/ref 不存在）——环境不可判（exit 2）。"""


def read_seed_versions(seed_dir: Path) -> tuple[dict[str, str], list[str]]:
    """种子树逐插件 version 表 + 读取错误明细。

    仅收「含 plugin.json 的子目录」为插件；单文件与共享根目录跳过（非插件
    属预期）。plugin.json 不可读/缺 version 记入错误明细（红面，不静默）。
    """
    versions: dict[str, str] = {}
    errors: list[str] = []
    for entry in sorted(seed_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{entry.name}/plugin.json 不可读：{exc}")
            continue
        ver = data.get("version") if isinstance(data, dict) else None
        if not isinstance(ver, str) or not ver:
            errors.append(f"{entry.name}/plugin.json 缺 version 字符串")
            continue
        versions[entry.name] = ver
    return versions, errors


def read_ref_versions(repo: Path, ref: str, root_rel: Path) -> tuple[dict[str, str], list[str]]:
    """git 侧（ref 默认 HEAD）同构 version 表：ls-tree 列目录 + show 取 plugin.json。

    ref 下无 plugin.json 的目录 = 共享根等非插件条目，与包侧同语义跳过。
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "--name-only", "-z", ref, "--", f"{root_rel.as_posix()}/"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise RefReadError(f"git ls-tree {ref} 失败（ref 不可解析或非 git 仓）：{detail}")
    prefix = root_rel.as_posix() + "/"
    versions: dict[str, str] = {}
    errors: list[str] = []
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode("utf-8", "replace")
        if not rel.startswith(prefix):
            continue
        name = rel[len(prefix) :]
        if not name or "/" in name:
            continue
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{prefix}{name}/plugin.json"],
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            continue
        try:
            data = json.loads(blob.stdout.decode("utf-8"))
        except json.JSONDecodeError:
            errors.append(f"{ref}:{prefix}{name}/plugin.json 非 JSON")
            continue
        ver = data.get("version") if isinstance(data, dict) else None
        if not isinstance(ver, str) or not ver:
            errors.append(f"{ref}:{prefix}{name}/plugin.json 缺 version 字符串")
            continue
        versions[name] = ver
    return versions, errors


def compare_versions(pack: dict[str, str], ref_side: dict[str, str], ref_display: str) -> list[str]:
    """逐插件对账（纯函数，可测）：版本不等 + 两侧缺失，返回差异表行（空=一致）。"""
    lines: list[str] = []
    for pid in sorted(set(pack) & set(ref_side)):
        if pack[pid] != ref_side[pid]:
            lines.append(f"版本不等：{pid} 包内种子={pack[pid]} 仓内({ref_display})={ref_side[pid]}")
    for pid in sorted(set(pack) - set(ref_side)):
        lines.append(f"包内种子多余（仓内 {ref_display} 无此插件，疑似陈旧残留）：{pid} 种子版本={pack[pid]}")
    for pid in sorted(set(ref_side) - set(pack)):
        lines.append(f"包内种子缺失（漏拷/陈旧包）：{pid} 仓内({ref_display})版本={ref_side[pid]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="包内插件种子新鲜度闸（种子版本 == 仓内 ref，ADR 2026-11-07 刀 4）"
    )
    parser.add_argument(
        "--pack-root", type=Path, required=True, help="打包产物根（含 plugins/shared 子树：worktree 或已装 resources 根）"
    )
    parser.add_argument("--repo", type=Path, default=_REPO_DEFAULT, help="仓库根（默认脚本所在仓）")
    parser.add_argument("--ref", default="HEAD", help="仓内参照提交（默认 HEAD；release 链传 BUILD_HEAD）")
    args = parser.parse_args(argv)

    pack_root = args.pack_root.resolve()
    seed_dir = pack_root / _SEED_REL
    if not pack_root.is_dir():
        print(f"[plugin-freshness] FAIL 环境不可判：pack-root 不存在：{pack_root}")
        return 2
    if not seed_dir.is_dir():
        print(f"[plugin-freshness] FAIL 打包产物无种子树：{seed_dir}（pack-root 传错或包残缺）")
        return 1

    try:
        pack_versions, pack_errors = read_seed_versions(seed_dir)
    except OSError as exc:
        print(f"[plugin-freshness] FAIL 环境不可判：种子树不可读：{exc}")
        return 2
    try:
        ref_versions, ref_errors = read_ref_versions(args.repo.resolve(), args.ref, _SEED_REL)
    except RefReadError as exc:
        print(f"[plugin-freshness] FAIL 环境不可判：{exc}")
        return 2

    print(
        f"[plugin-freshness] 对账面：包内种子 {len(pack_versions)} 插件 ↔ 仓内 {args.ref} {len(ref_versions)} 插件"
    )
    diff = compare_versions(pack_versions, ref_versions, args.ref)
    errors = pack_errors + ref_errors
    if diff or errors:
        if errors:
            print(f"[plugin-freshness] FAIL 清单读取错误（{len(errors)} 项）：")
            for line in errors:
                print(f"[plugin-freshness]   - {line}")
        if diff:
            print(f"[plugin-freshness] FAIL 差异表（{len(diff)} 项）：")
            for line in diff:
                print(f"[plugin-freshness]   - {line}")
            print("[plugin-freshness] 动作：pack-root 种子树须与仓内提交同源重烘（worktree checkout")
            print("       --force BUILD_HEAD 后重打包；ADR 2026-11-07 §2.3 陈旧包打不出来）")
        return 1
    print(f"[plugin-freshness] PASS 包内种子新鲜（{len(pack_versions)} 插件版本 == 仓内 {args.ref}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
