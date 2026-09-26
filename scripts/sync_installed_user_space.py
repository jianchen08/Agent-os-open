#!/usr/bin/env python3
"""开发侧一键更新装机版用户空间（repo → 装机版 user_root，不动装机包）。

背景（ADR 2026-09-24-host-context-generic-connection 落地日）：插件更新此前只有
两条路——launcher 启动期同步（只服务 dev user_root）或整机重打包（132MB，且
装机包内文件被改动即破坏升级链）。本脚本补第三条：**显式把仓内插件/配置/
宿主 addon 增量带到装机版的用户空间**，内核经用户插件根递归发现（同 id 用户
赢）与用户 config 覆盖（config/pipelines/*）天然消费，装机包一个字节不动。

写目标必须显式（与 sync_user_root.py 同一纪律）：``--user-root`` 是唯一入口，
不给拒绝执行——OS 默认数据目录（%APPDATA%\\agentos）是装机版用户空间，静默
写入违反装机/开发零共享（用户裁定 2026-09-18）。目标落在装机包安装目录内
一律拒绝（检测 %LOCALAPPDATA%\\Programs\\agent-os 等安装位）。

三个动作（至少给一个）：
- ``--plugin <repo相对路径>``：镜像同步插件目录到 ``<user_root>/plugins/<名>/``
  （镜像=删除源里已消失的文件，改名类更新必须；``data/`` 用户数据子目录
  保留不删；排 ``__pycache__``）。venv 供给：源侧有 ``.venv`` 直接整树复制
  （同机可迁移，editable SDK 绝对路径仍有效）；无则按内核报错同款配方
  ``uv venv + uv pip install -e plugins/sdk + 清单依赖`` 现建。
- ``--config <config/下相对路径>``：整文件覆盖到 ``<user_root>/config/<同路径>``
  （用户空间配置惯例=文件级整体替换；如 pipelines/autonomous.yaml 改挂
  pipeline_host_context）。
- ``--addon <Godot项目根>``：把 hosts/godot-addons/agentos 镜像到
  ``<项目>/addons/agentos``——修旧项目播种副本推死端点（插件改名后旧端点
  404）的存量问题。

契约提醒：涉及端点/WS 事件名等**前端契约**的插件更新，装机版内嵌前端（asar）
要随应用升级才能消费——本脚本只更新 Python 侧，届时 --config 换挂需与
应用升级同批（换挂后旧前端引用链断，属诚实可见的破坏性切换）。

用法：
  python scripts/sync_installed_user_space.py --user-root "%APPDATA%\\agentos" \\
      --plugin shared/pipeline/input/host_context
  python scripts/sync_installed_user_space.py --user-root "%APPDATA%\\agentos" \\
      --config pipelines/autonomous.yaml --config kernel/default_profile.yaml
  python scripts/sync_installed_user_space.py --addon "D:\\my-godot-project"
  任意动作加 --dry-run 只列计划不落盘。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 镜像同步排除的目录名（按名精确匹配）：字节码缓存与 sidecar 虚拟环境
#: （venv 由 _provision_venv 单独供给，不入镜像）。
EXCLUDED_DIR_NAMES = ("__pycache__", ".venv")

#: 插件内用户数据子目录名：镜像删除时保留（用户活跃数据，非仓从属物）。
PRESERVED_USER_DATA_DIRS = ("data",)

#: 已知装机包安装位（相对 %LOCALAPPDATA% / Program Files 的候选）：目标落在
#: 这些目录子树内一律拒绝——「不动装机版」是本脚本的硬边界。
_INSTALL_DIR_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "agent-os",
    Path("C:/Program Files/agent-os"),
    Path("C:/Program Files (x86)/agent-os"),
)


def _existing_install_dirs() -> list[Path]:
    return [p.resolve() for p in _INSTALL_DIR_CANDIDATES if p.is_dir()]


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def resolve_user_root(args_user_root: str | None) -> Path:
    """解析并校验写目标：--user-root 必给；不得落在装机包安装目录内。"""
    if not args_user_root:
        raise SystemExit(
            "[REFUSE] 写目标必须显式：--user-root <装机版用户空间根>。\n"
            "  装机版默认: %APPDATA%\\agentos；dev 自有 user_root 不在本脚本职责内\n"
            "  （launcher 启动期已自动同步）。\n"
            "  静默写装机版用户空间违反装机/开发零共享（用户裁定 2026-09-18）。"
        )
    root = Path(args_user_root).resolve()
    for install_dir in _existing_install_dirs():
        if _is_under(root, install_dir):
            raise SystemExit(
                f"[REFUSE] 目标 {root} 落在装机包安装目录 {install_dir} 内——"
                "本脚本只写用户空间，绝不动装机包。"
            )
    return root


def _mirror_tree(src: Path, dst: Path, *, preserve: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """镜像 src→dst：复制/覆盖源侧文件，删除源里已消失的条目。

    返回 [(动作, 相对路径)]（copy/delete）。PRESERVE 名单内的 dst 子树只增
    不删（用户数据）。调用方负责排除目录（.venv 等不入镜像）。
    """
    actions: list[tuple[str, str]] = []
    dst.mkdir(parents=True, exist_ok=True)

    for cur_src, _dirs, files in os.walk(src):
        src_dir = Path(cur_src)
        rel = src_dir.relative_to(src)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        cur_dst = dst / rel
        cur_dst.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith(".pyc"):
                continue
            shutil.copy2(src_dir / f, cur_dst / f)
            actions.append(("copy", str((rel / f).as_posix())))

    # 反向：dst 独有且源侧已消失的文件/目录 → 删除（preserve 名单跳过）
    for cur_dst_dir, dirs, files in os.walk(dst):
        dst_dir = Path(cur_dst_dir)
        rel = dst_dir.relative_to(dst)
        if any(part in PRESERVED_USER_DATA_DIRS for part in rel.parts):
            dirs[:] = []  # 整子树保留，不再下探删除
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        for f in list(files):
            if not (src / rel / f).exists():
                (dst_dir / f).unlink()
                actions.append(("delete", str((rel / f).as_posix())))
        for d in list(dirs):
            if d in PRESERVED_USER_DATA_DIRS or d == ".venv":
                continue
            if not (src / rel / d).exists():
                shutil.rmtree(dst_dir / d)
                dirs.remove(d)
                actions.append(("delete", str((rel / d).as_posix())))
    return actions


def _provision_venv(src_plugin: Path, dst_plugin: Path, dry_run: bool) -> str:
    """dst venv 供给：优先整树复制源 .venv（同机 editable SDK 路径有效）；
    源无 .venv 则走 uv 现建（内核 502 报错同款配方）。"""
    dst_venv = dst_plugin / ".venv"
    src_venv = src_plugin / ".venv"
    if dst_venv.exists():
        return "venv: 已存在，跳过"
    if src_venv.exists():
        if dry_run:
            return f"venv: [dry-run] 复制 {src_venv}"
        shutil.copytree(src_venv, dst_venv, symlinks=True)
        return f"venv: 复制自 {src_venv}"
    if dry_run:
        return "venv: [dry-run] uv venv + uv pip install -e plugins/sdk + 清单依赖"
    subprocess.run(
        ["uv", "venv", "--python", "3.12"],
        cwd=dst_plugin, check=True, capture_output=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(dst_venv / "Scripts" / "python.exe"),
         "-e", str(_REPO_ROOT / "plugins" / "sdk")],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(dst_venv / "Scripts" / "python.exe"), "aiohttp"],
        check=True, capture_output=True,
    )
    return "venv: uv 现建（sdk editable + aiohttp）"


def sync_plugin(repo_rel: str, user_root: Path, dry_run: bool) -> None:
    """插件目录镜像到 <user_root>/plugins/<名>/ + venv 供给。"""
    src = _REPO_ROOT / "plugins" / repo_rel.replace("\\", "/")
    if not (src / "plugin.json").exists():
        raise SystemExit(f"[REFUSE] {src} 不含 plugin.json——--plugin 传仓内插件目录相对路径")
    dst = user_root / "plugins" / src.name
    print(f"[plugin] {src.name}: {src} -> {dst}")
    if not dry_run:
        actions = _mirror_tree(src, dst)
        for act, rel in actions:
            print(f"    {act}: {rel}")
        # venv 子树在镜像之外单独供给（排除名单使其不参与 mirror）
        print(f"    {_provision_venv(src, dst, dry_run)}")
    else:
        print("    [dry-run] 镜像 + venv 供给（详见源侧文件树）")
    print(
        "    [NOTE] 契约提醒：若本次更新改了端点/WS 事件名等前端契约，"
        "装机版内嵌前端需随应用升级才能消费；--config 换挂须与升级同批评估。"
    )


def sync_config(repo_rel: str, user_root: Path, dry_run: bool) -> None:
    """config/ 下单文件整覆盖到 <user_root>/config/<同路径>。"""
    src = _REPO_ROOT / "config" / repo_rel.replace("\\", "/")
    if not src.is_file():
        raise SystemExit(f"[REFUSE] {src} 不存在——--config 传 config/ 下相对路径")
    dst = user_root / "config" / src.relative_to(_REPO_ROOT / "config")
    print(f"[config] {src} -> {dst}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def sync_addon(project_root: str, dry_run: bool) -> None:
    """宿主 addon 镜像到 <Godot项目>/addons/agentos（修存量项目死端点）。"""
    src = _REPO_ROOT / "hosts" / "godot-addons" / "agentos"
    if not src.is_dir():
        raise SystemExit(f"[REFUSE] {src} 不存在")
    dst = Path(project_root) / "addons" / "agentos"
    print(f"[addon] {src} -> {dst}")
    if dry_run:
        print("    [dry-run] 镜像同步")
        return
    if not (dst.parent / "project.godot").exists() and not Path(project_root, "project.godot").exists():
        print("    [WARN] 目标不含 project.godot——确认这真是 Godot 项目根")
    for act, rel in _mirror_tree(src, dst):
        print(f"    {act}: {rel}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="开发侧一键更新装机版用户空间（插件/配置/宿主addon），不动装机包",
    )
    parser.add_argument("--user-root", help="装机版用户空间根（必填才可写，如 %%APPDATA%%\\agentos）")
    parser.add_argument("--plugin", action="append", default=[],
                        help="仓内插件目录（plugins/ 下相对路径），可多次")
    parser.add_argument("--config", action="append", default=[],
                        help="config/ 下相对路径单文件，可多次")
    parser.add_argument("--addon", help="Godot 项目根（同步 hosts/godot-addons/agentos 到其 addons/）")
    parser.add_argument("--dry-run", action="store_true", help="只列计划不落盘")
    args = parser.parse_args(argv)

    if not (args.plugin or args.config or args.addon):
        parser.error("至少给一个动作：--plugin / --config / --addon（--dry-run 预览同样要先选动作）")

    if args.addon and not (args.plugin or args.config):
        # addon 目标是外部 Godot 项目，与 user_root 无关——允许不给 --user-root
        sync_addon(args.addon, args.dry_run)
        return 0

    user_root = resolve_user_root(args.user_root)
    print(f"[target] 用户空间: {user_root}" + ("  [dry-run]" if args.dry_run else ""))
    for rel in args.plugin:
        sync_plugin(rel, user_root, args.dry_run)
    for rel in args.config:
        sync_config(rel, user_root, args.dry_run)
    if args.addon:
        sync_addon(args.addon, args.dry_run)
    print("[OK] 完成。内核重启或插件热发现后生效（配置覆盖需重启内核的步骤以装机版形态为准）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
