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

动作（至少给一个）：
- ``--plugin <repo相对路径>``：镜像同步插件目录到 ``<user_root>/plugins/<名>/``
  （镜像=删除源里已消失的文件，改名类更新必须；``data/`` 用户数据子目录
  保留不删；排 ``__pycache__``）。venv 供给：源侧有 ``.venv`` 直接整树复制
  （同机可迁移，editable SDK 绝对路径仍有效）；无则按内核报错同款配方
  ``uv venv + uv pip install -e plugins/sdk + 清单依赖`` 现建。
  版本生效检查（D4）：BuiltinFirst 下源版本须严格大于装机包内置副本
  （宽容 strip ``v`` 前缀），否则该 id 进汇总的「未生效 id 清单」。
- ``--config <config/下相对路径>``：整文件覆盖到 ``<user_root>/config/<同路径>``
  （用户空间配置惯例=文件级整体替换；如 pipelines/autonomous.yaml 改挂
  pipeline_host_context）。
- ``--skills [skills/下相对路径]``：skills/ 子树镜像到 ``<user_root>/skills/``
  （缺省整库）——装机包 resources/ 不含技能权威源（extraResources 未收录），
  仓根技能经此补种到用户空间，装机版会话工作区从该源同步全量技能（R300
  悬空引用根治的存量装机通道）。
- ``--addon <Godot项目根>``：把 hosts/godot-addons/agentos 镜像到
  ``<项目>/addons/agentos``——修旧项目播种副本推死端点（插件改名后旧端点
  404）的存量问题。
- ``--prune-stale``：存量归一：用户空间里 manifest id 命中内置副本且版本
  ``<=`` 内置版本的目录永不被装载（BuiltinFirst 持平/更旧内置赢），移入
  ``<user_root>/_quarantine_<日期>/plugins/`` 并记 prune-manifest.json
  （可整体回滚）；内置无同名 id 的用户自有插件只报告不动。

契约提醒：涉及端点/WS 事件名等**前端契约**的插件更新，装机版内嵌前端（asar）
要随应用升级才能消费——本脚本只更新 Python 侧，届时 --config 换挂需与
应用升级同批（换挂后旧前端引用链断，属诚实可见的破坏性切换）。

用法：
  python scripts/sync_installed_user_space.py --user-root "%APPDATA%\\agentos" \\
      --plugin host_context
  python scripts/sync_installed_user_space.py --user-root "%APPDATA%\\agentos" \\
      --config pipelines/autonomous.yaml --config kernel/default_profile.yaml
  python scripts/sync_installed_user_space.py --user-root "%APPDATA%\\agentos" --skills
  python scripts/sync_installed_user_space.py --addon "D:\\my-godot-project"
  任意动作加 --dry-run 只列计划不落盘。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# scripts/ 非包（平铺脚本目录）：注入自身目录以抽 import 复用 runtime health
# 探测件（E4-P0 孤儿 import 冒烟；勿复制粘贴——2026-11-05 任务裁定）。
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_plugin_runtime_health import _default_runner, find_venv_python  # noqa: E402

#: 镜像同步排除的目录名（按名精确匹配）：字节码缓存与 sidecar 虚拟环境
#: （venv 由 _provision_venv 单独供给，不入镜像）。
EXCLUDED_DIR_NAMES = ("__pycache__", ".venv")

#: 插件内用户数据子目录名：镜像删除时保留（用户活跃数据，非仓从属物）。
PRESERVED_USER_DATA_DIRS = ("data",)

#: 开发机绝对路径指纹：随 sync 进用户空间即复现“提示词锚定 dev 路径 →
#: 隔离挂载悬空”链（2026-09-27 R295 装机实证）。检测优先于替换：替换规则
#: 会失配静默空转（sync_open_repo 的 isolation root 替换实证），命中即拒。
DEV_PATH_PATTERNS = ("d:\\myproject", "d:/myproject", "container_e17cc5927dfd")

#: 已知装机包安装位（相对 %LOCALAPPDATA% / Program Files 的候选）：目标落在
#: 这些目录子树内一律拒绝——「不动装机版」是本脚本的硬边界。
_INSTALL_DIR_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "agent-os",
    Path("C:/Program Files/agent-os"),
    Path("C:/Program Files (x86)/agent-os"),
)


def _existing_install_dirs() -> list[Path]:
    dirs = [p.resolve() for p in _INSTALL_DIR_CANDIDATES if p.is_dir()]
    # 安装位不在默认候选（自定义 /D 安装，如 D:\agentos-r297-main）时经环境
    # 变量显式供给；NSIS 卸载注册表无 InstallLocation 可依赖（实证）。
    extra = os.environ.get("AGENTOS_INSTALL_DIR", "").strip()
    if extra:
        p = Path(extra)
        if p.is_dir() and p.resolve() not in dirs:
            dirs.append(p.resolve())
    return dirs


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
                f"[REFUSE] 目标 {root} 落在装机包安装目录 {install_dir} 内——本脚本只写用户空间，绝不动装机包。"
            )
    return root


def _mirror_tree(
    src: Path,
    dst: Path,
    *,
    preserve: tuple[str, ...] = (),
    skip_suffixes: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """镜像 src→dst：复制/覆盖源侧文件，删除源里已消失的条目。

    返回 [(动作, 相对路径)]（copy/delete）。PRESERVE 名单内的 dst 子树只增
    不删（用户数据）。调用方负责排除目录（.venv 等不入镜像）；
    skip_suffixes 命中的源文件不复制、dst 侧对应残留删除（按名精确匹配，
    供技能面剔除 .bak/.tmp 编辑残渣）。
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
            if f.endswith(".pyc") or f.lower().endswith(skip_suffixes):
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
            if not (src / rel / f).exists() or f.lower().endswith(skip_suffixes):
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


# editable 可归一白名单：dist 名 → 仓内源目录（路径相对 _REPO_ROOT）。
# 名单外 editable 不猜来源，留报告人工裁决。
_EDITABLE_SOURCES = {
    "agentos-plugin-sdk": "plugins/sdk",
    "agentos-builtin-tools": "plugins/shared/tools/builtin_tools",
}


def _venv_python(venv: Path) -> Path:
    for rel in ("Scripts/python.exe", "bin/python"):
        candidate = venv / rel
        if candidate.is_file():
            return candidate
    return venv / "Scripts" / "python.exe"


def _venv_site_packages(venv: Path) -> list[Path]:
    """双布局 site-packages（Windows Lib/ 与 POSIX lib/python*/）。"""
    sites = [venv / "Lib" / "site-packages"]
    sites.extend(sorted((venv / "lib").glob("python*/site-packages")))
    return [s for s in sites if s.is_dir()]


def _heal_sdk_editable(dst_venv: Path, dry_run: bool) -> str | None:
    """editable→实体归一（装机/开发运行时隔离铁律的供给面执法）。

    整树复制与 uv ``-e`` 两条供给路径都会带入 editable 安装（__editable__*.pth
    指向源仓）——源仓是受管工作区（周期性还原 git HEAD），还原窗口内装机侧
    集体悬空（2026-10-29 装机实证 46 插件 No module named
    agentos_plugin_sdk，llm litellm 半初始化连锁）。白名单内 dist 卸载
    editable 副本、从源目录实体安装（wheel 落进 venv，无路径耦合）；名单外
    editable 留报告人工裁决。"""
    outcomes: list[str] = []
    for site in _venv_site_packages(dst_venv):
        for pth in sorted(site.glob("__editable__*.pth")):
            # 文件名形态：__editable__.<dist>-<version>.pth
            stem = pth.stem.removeprefix("__editable__.").strip("_")
            dist_name = stem.rsplit("-", 1)[0] if "-" in stem else stem
            dist_norm = re.sub(r"[-_.]+", "-", dist_name.lower())
            source_rel = _EDITABLE_SOURCES.get(dist_norm)
            if source_rel is None:
                outcomes.append(f"editable 白名单外待人工裁决 {pth.name}")
                continue
            if dry_run:
                outcomes.append(f"[dry-run] {dist_name} editable→实体归一")
                continue
            python = _venv_python(dst_venv)
            subprocess.run(
                ["uv", "pip", "uninstall", "--python", str(python), dist_name],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    str(_REPO_ROOT / source_rel),
                ],
                check=True,
                capture_output=True,
            )
            outcomes.append(f"{dist_name} editable→实体归一")
    if not outcomes:
        return None
    return "venv: " + "；".join(outcomes)


def _provision_venv(src_plugin: Path, dst_plugin: Path, dry_run: bool) -> str:
    """dst venv 供给：优先整树复制源 .venv（随后 SDK 归一为实体安装）；
    源无 .venv 则走 uv 现建（实体 SDK + 清单依赖）。"""
    dst_venv = dst_plugin / ".venv"
    src_venv = src_plugin / ".venv"
    if dst_venv.exists():
        healed = _heal_sdk_editable(dst_venv, dry_run)
        return "venv: 已存在，跳过" if healed is None else f"已存在；{healed}"
    if src_venv.exists():
        if dry_run:
            return f"venv: [dry-run] 复制 {src_venv} + SDK 实体归一"
        shutil.copytree(src_venv, dst_venv, symlinks=True)
        healed = _heal_sdk_editable(dst_venv, dry_run)
        suffix = f"；{healed}" if healed else ""
        return f"venv: 复制自 {src_venv}{suffix}"
    if dry_run:
        return "venv: [dry-run] uv venv + SDK 实体安装 + aiohttp"
    subprocess.run(
        ["uv", "venv", "--python", "3.12"],
        cwd=dst_plugin,
        check=True,
        capture_output=True,
    )
    dst_python = dst_venv / "Scripts" / "python.exe"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(dst_python),
            str(_REPO_ROOT / "plugins" / "sdk"),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(dst_python), "aiohttp"],
        check=True,
        capture_output=True,
    )
    return "venv: uv 现建（SDK 实体安装 + aiohttp）"


# 探针模板：在生效副本目录上真实 import 依赖并逐符号 hasattr（真实解析，
# 含 PEP 562 动态转发）；导入失败以 IMPORT_ERROR: 前缀与缺符号区分。
_CLOSURE_PROBE_TEMPLATE = """import importlib, sys
try:
    mod = importlib.import_module({dep_name!r})
except Exception as e:
    print('IMPORT_ERROR:' + repr(e))
else:
    missing = [s for s in {symbols!r} if not hasattr(mod, s)]
    print(missing)
"""


def check_import_closure(
    src: Path,
    user_plugins: Path,
    install_roots: list[Path] | None = None,
    timeout_secs: float = 60.0,
) -> str | None:
    """跨插件 import 闭包闸：src 的 server.py/plugin.py 中 ``from <共享根内插件>
    import 符号`` 逐个在生效副本上验证「模块可导入 + 符号在位」。

    返回首个不满足的依赖描述（str）或 None（自洽/无跨插件依赖）。仅覆盖裸名
    跨插件 from-import（2026-10-17 装机事故形态：task_submit ``from _root_api
    import WORKSPACE_PREPARE_PHASE`` × _root_api 未随同更新 → 生产 ImportError
    合宿组全灭）；动态导入与插件内引用不在闸内。导入探测在生效副本目录上
    真实执行（hasattr 走模块真实解析，含 PEP 562 转发），fail-closed。
    """
    dep_syms: dict[str, set[str]] = {}
    for filename in ("server.py", "plugin.py"):
        f = src / filename
        if not f.is_file():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError:
            continue  # 语法错误非本闸职责（加载期自然暴露）
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "." not in node.module:
                # __future__ 是编译器指令非跨插件依赖（server.py/plugin.py 常备
                # annotations 导入，误收=任何插件 sync 恒报闭包不满足）
                if node.module == "__future__":
                    continue
                names = [a.name for a in node.names if a.name != "*"]
                # 插件目录内裸模块（如 trace_export_core 随包同目录）经
                # bootstrap 注入的插件目录 sys.path 解析，非跨插件依赖；
                # 误收会在 plugins 根 cwd 上探测必败（2026-10-23 实证）。
                in_plugin_dir = (src / f"{node.module}.py").is_file() or (src / node.module).is_dir()
                if names and node.module != src.name and not in_plugin_dir:
                    dep_syms.setdefault(node.module, set()).update(names)
    if not dep_syms:
        return None
    roots = [Path(r) for r in install_roots] if install_roots is not None else _existing_install_dirs()
    for dep_name, symbols in sorted(dep_syms.items()):
        dep_dir: Path | None = None
        builtin_dir: Path | None = None
        builtin_version: str | None = None
        for root in roots:
            cand = root / "resources" / "plugins" / "shared" / dep_name
            # 共享裸模块（_root_api/_libs 等）无 plugin.json 也是合法内置副本；
            # 只认 manifest 会让闭包闸对「server.py from _root_api import …」
            # 恒报不满足（trace_exporter 2026-10-23 实证）。无 manifest →
            # version=None，用户副本 semver 可解析即赢（与 parse 容 None 口径一致）。
            if cand.is_dir():
                builtin_dir = cand
                builtin_version = _read_manifest_version(cand)
                break
        user_dir = user_plugins / dep_name
        user_version = _read_manifest_version(user_dir) if (user_dir / "plugin.json").is_file() else None
        if user_dir.is_dir() and user_version:
            user_key = parse_semver_lenient(user_version)
            builtin_key = parse_semver_lenient(builtin_version) if builtin_version else None
            if user_key is not None and (builtin_key is None or user_key > builtin_key):
                dep_dir = user_dir
        dep_dir = dep_dir or builtin_dir
        probe_exe = sys.executable
        probe_cwd: Path | None
        if dep_dir is None:
            # 第三候选：插件自带 venv 内解析（editable path 依赖如
            # agentos_plugin_sdk 无副本形态，venv 装配即用——与生产加载同环境，
            # 探测失败仍 fail-closed）。
            venv_py = next(
                (
                    p
                    for p in (
                        src / ".venv" / "Scripts" / "python.exe",
                        src / ".venv" / "bin" / "python",
                    )
                    if p.is_file()
                ),
                None,
            )
            if venv_py is None:
                return f"{src.name}: 依赖闭包不满足——跨插件依赖 {dep_name} 在用户空间与安装目录均未找到，且源侧无 venv 可探测"
            probe_exe = str(venv_py)
            probe_cwd = None
        else:
            probe_cwd = dep_dir.parent
        probe = _CLOSURE_PROBE_TEMPLATE.format(dep_name=dep_name, symbols=sorted(symbols))
        try:
            proc = subprocess.run(
                [probe_exe, "-c", probe],
                check=False,
                cwd=str(probe_cwd) if probe_cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout_secs,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return (
                f"{src.name}: 依赖闭包检查超时——{dep_name} 导入探测 "
                f"{timeout_secs}s 未返回（探测环境与生产加载环境不一致时人工复核）"
            )
        out = (proc.stdout or "").strip()
        if out.startswith("IMPORT_ERROR:") or proc.returncode != 0:
            detail = (
                out[len("IMPORT_ERROR:") :].strip()
                if out.startswith("IMPORT_ERROR:")
                else out.splitlines()[-1]
                if out
                else f"exit={proc.returncode}"
            )
            return f"{src.name}: 依赖闭包不满足——{dep_name} 生效副本 ({dep_dir}) 导入失败: {detail}"
        missing = out
        if missing and missing != "[]":
            return f"{src.name}: 依赖闭包不满足——{dep_name} 生效副本 ({dep_dir}) 缺符号 {missing}"
    return None


def sync_plugin(repo_rel: str, user_root: Path, dry_run: bool) -> str | None:
    """插件目录镜像到 <user_root>/plugins/<名>/ + venv 供给。

    返回未生效原因（str）或 None（生效/无法判定）——BuiltinFirst 裁决口径下
    用户副本不严格大于内置（或版本不可解析）时，本轮 sync 不会生效（D4/
    审查总册 P1-15：失效必须显式可见，不再静默）。
    """
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
    reason = check_version_effective(src)
    if reason:
        print(f"    [WARN] 未生效: {reason}")
    closure = check_import_closure(src, user_root / "plugins")
    if closure:
        print(f"    [WARN] 依赖闭包: {closure}")
        reason = reason or closure
    print(
        "    [NOTE] 契约提醒：若本次更新改了端点/WS 事件名等前端契约，"
        "装机版内嵌前端需随应用升级才能消费；--config 换挂须与升级同批评估。"
    )
    return reason


#: 宽容 semver 形状：MAJOR.MINOR.PATCH[-预发布][+构建]（构建元数据不参与比较）。
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


def parse_semver_lenient(raw: str) -> tuple[int, int, int, int, tuple] | None:
    """宽容 semver 解析（与内核 D4 口径同源）：strip 空白与单个前导 ``v``/``V``
    前缀，``v1.2.3`` 不再静默判旧；返回可比较键（正式版优先于一切预发布），
    失败 None。"""
    s = raw.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    m = _SEMVER_RE.fullmatch(s)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre_raw = m.group(4)
    if pre_raw is None:
        return (major, minor, patch, 1, ())
    # semver 预发布优先级：数字标识符 < 字母数字标识符（编码 0/1 保证）
    pre_key = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre_raw.split("."))
    return (major, minor, patch, 0, pre_key)


def _read_manifest_version(plugin_dir: Path) -> str | None:
    """读 plugin.json 的 ``version``；缺失/损坏 → None。"""
    try:
        data = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def _builtin_plugin_version(plugin_name: str) -> str | None:
    """定位装机包内置副本版本（``<安装位>/resources/plugins/shared/<名>/``，
    与 electron kernelResourcePaths 的 builtin 布局同源）；找不到 → None。"""
    for install_dir in _existing_install_dirs():
        manifest = install_dir / "resources" / "plugins" / "shared" / plugin_name
        if (manifest / "plugin.json").is_file():
            return _read_manifest_version(manifest)
    return None


def check_version_effective(src: Path) -> str | None:
    """用户副本按 BuiltinFirst 口径能否生效：返回未生效原因或 None。

    ``src`` 为源侧插件目录（目录名承载插件名）。判据与内核仲裁逐位同源
    （``parse_version`` 宽容 strip v 前缀 + 严格大于内置）：源版本不可解析 /
    内置版本不可解析 / 不严格大于内置 → 未生效。装机包未安装（找不到内置
    副本）→ 无法判定，不入清单。
    """
    src_version = _read_manifest_version(src)
    if src_version is None:
        return f"{src.name}: 源 plugin.json 无可读 version"
    src_key = parse_semver_lenient(src_version)
    if src_key is None:
        return f"{src.name}: 源版本 {src_version!r} 非 semver（宽容 strip v 前缀后仍不可解析）——仲裁保守判内置赢"
    builtin_version = _builtin_plugin_version(src.name)
    if builtin_version is None:
        return None  # 装机包未装/布局外 → 无法判定
    builtin_key = parse_semver_lenient(builtin_version)
    if builtin_key is None:
        return f"{src.name}: 内置版本 {builtin_version!r} 不可解析——仲裁保守判内置赢（请修装机包副本）"
    if src_key <= builtin_key:
        return (
            f"{src.name}: 源版本 {src_version} 不严格大于内置 {builtin_version}"
            "（BuiltinFirst 持平/更旧内置赢）——bump 版本后重试"
        )
    return None


def _assert_no_dev_paths(src: Path) -> None:
    """文本源文件含开发机绝对路径时拒绝同步（fail-closed，防止污染用户空间）。"""
    if src.suffix.lower() not in (".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".py"):
        return
    try:
        lines = src.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return
    for i, line in enumerate(lines, 1):
        low = line.lower()
        if any(pat in low for pat in DEV_PATH_PATTERNS):
            raise SystemExit(
                f"[REFUSE] {src}:{i} 含开发机绝对路径（{line.strip()[:80]}）——"
                "可变路径用占位符（{{user_root}} 等），真值落用户空间"
            )


def sync_config(repo_rel: str, user_root: Path, dry_run: bool) -> None:
    """config/ 下单文件整覆盖到 <user_root>/config/<同路径>。"""
    src = _REPO_ROOT / "config" / repo_rel.replace("\\", "/")
    if not src.is_file():
        raise SystemExit(f"[REFUSE] {src} 不存在——--config 传 config/ 下相对路径")
    _assert_no_dev_paths(src)
    dst = user_root / "config" / src.relative_to(_REPO_ROOT / "config")
    print(f"[config] {src} -> {dst}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


#: 技能镜像剔除的文件后缀（prompt 注入物料不留编辑残渣；.bak 历史副本
#: 常含开发机旧路径，R295 同类污染源）。
SKILLS_SKIP_SUFFIXES = (".bak", ".tmp")


def sync_skills(repo_rel: str, user_root: Path, dry_run: bool) -> None:
    """skills/ 子树镜像到 <user_root>/skills/<同路径>（装机根技能权威源）。

    装机包 resources/ 不含仓根 skills/（extraResources 未收录，R300 实证
    agentos.yaml 引用悬空），装机版会话工作区经 _skill_sources 的用户根
    skills/ 源从此目录拿全量技能。镜像语义与 --plugin 同（源里已消失的
    条目删除）；文本物料逐一过开发机路径指纹检查（技能会被 file_read 注入
    提示词，dev 绝对路径进用户空间即复现 R295「提示词锚定 dev 路径」链）。
    """
    skills_root = _REPO_ROOT / "skills"
    src = skills_root / repo_rel.replace("\\", "/")
    if not src.is_dir():
        raise SystemExit(f"[REFUSE] {src} 不存在——--skills 传 skills/ 下相对路径（整库传 .）")
    for f in sorted(src.rglob("*")):
        if f.is_file() and f.suffix.lower() in (".md", ".yaml", ".yml", ".txt"):
            _assert_no_dev_paths(f)
    dst = user_root / "skills"
    if src != skills_root:
        dst = dst / src.relative_to(skills_root)
    print(f"[skills] {src} -> {dst}")
    if not dry_run:
        for act, rel in _mirror_tree(src, dst, skip_suffixes=SKILLS_SKIP_SUFFIXES):
            print(f"    {act}: {rel}")


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


#: 隔离目录名前缀（用户空间根下；同一轮清理的副本集中放这里，可整体回滚）
QUARANTINE_PREFIX = "_quarantine_"

#: 插件清单遍历剪枝目录名（venv/缓存/用户数据不下钻）
_MANIFEST_WALK_SKIP = (".venv", "__pycache__", "node_modules", ".git", "data")


def _iter_plugin_manifests(root: Path, *, max_depth: int = 2, skip_modes: bool = False):
    """枚举插件清单目录（两层布局：``plugins/<id>/`` 与 ``plugins/<组>/<id>/``）。

    skip_modes=True 时跳过 ``modes`` 子树（ADR 2026-09-20：modes 账本自持
    「恒用户赢」仲裁，子树副本不进 prune 影子/孤儿处置——按版本比较隔离
    已定制模式副本是数据丢失风险路径，2026-11-05 装机现场实证）。
    """
    if not root.is_dir():
        return
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            children = sorted(p for p in cur.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if child.name in _MANIFEST_WALK_SKIP:
                continue
            if skip_modes and child.name == "modes":
                continue
            if (child / "plugin.json").is_file():
                yield child
            elif depth + 1 < max_depth:
                stack.append((child, depth + 1))


def _builtin_manifest_index(install_roots: list[Path] | None) -> dict[str, tuple[str | None, Path]]:
    """内置副本索引：插件 id → (版本, 目录)。

    id 取 manifest 的 ``id``（缺省回落目录名）——内核仲裁按 manifest id
    比版本，目录名仅作兜底。
    """
    roots = install_roots if install_roots is not None else _existing_install_dirs()
    index: dict[str, tuple[str | None, Path]] = {}
    for install_dir in roots:
        shared = install_dir / "resources" / "plugins" / "shared"
        for manifest_dir in _iter_plugin_manifests(shared):
            try:
                data = json.loads((manifest_dir / "plugin.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            raw_id = data.get("id")
            raw_version = data.get("version")
            pid = raw_id if isinstance(raw_id, str) else manifest_dir.name
            version = raw_version if isinstance(raw_version, str) else None
            index.setdefault(pid, (version, manifest_dir))
    return index


def _import_smoke_failure(manifest_dir: Path, timeout_secs: float = 60.0) -> str | None:
    """孤儿副本 import 冒烟探测：在副本 venv 解释器上真实 import 入口模块。

    返回失败描述（str）或 None（冒烟通过 / 判不了）。入口模块 = server.py
    优先（进程装载入口，连锁展开其对插件实现与跨插件依赖的 import），缺则
    plugin.py；两者皆缺或无 venv 解释器 → None（判不了）：隔离面宁可漏
    不可错杀，与「版本不可解析不删」同向。E3 遗孤形态（plugin.py 共享根
    from-import 断裂恒炸）在此暴露。
    """
    venv_python = find_venv_python(manifest_dir)
    if venv_python is None:
        return None
    if (manifest_dir / "server.py").is_file():
        entry = "server"
    elif (manifest_dir / "plugin.py").is_file():
        entry = "plugin"
    else:
        return None
    code = f"import sys; sys.path.insert(0, {str(manifest_dir)!r}); import {entry}"
    ok, detail = _default_runner(venv_python, [code], timeout_s=int(timeout_secs))
    return None if ok else detail


def prune_stale_plugins(
    user_root: Path,
    *,
    dry_run: bool = False,
    install_roots: list[Path] | None = None,
) -> list[dict[str, str]]:
    """隔离用户空间里永远赢不了的旧插件副本（BuiltinFirst 影子）与失效孤儿。

    判据（与内核仲裁同源）：用户副本 manifest id 命中内置副本，且版本
    ``<=`` 内置版本（持平=内置赢）→ 该副本永不被装载，属存量垃圾；内置
    无同名 id 的孤儿若 import 冒烟失败（内置已下架后 BuiltinFirst 下孤儿
    即唯一活跃候选，装载必炸——2026-11-05 装机现场判据回填），同样隔离。
    两类副本移入 ``<user_root>/_quarantine_<日期>/plugins/`` 并记
    prune-manifest.json，可整体回滚。内置无同名且 import 健康的目录**不删**
    （用户自有插件，仅报告）；版本不可解析 / 冒烟判不了（无 venv / 无入口）
    的副本也不删（判不了，交人工）。modes 子树恒用户赢（ADR 2026-09-20），
    不进任何处置。

    返回本轮处置清单（id/version/dir/why），dry-run 时为空列表但打印计划。
    """
    plugins_dir = user_root / "plugins"
    builtin = _builtin_manifest_index(install_roots)
    if not plugins_dir.is_dir():
        print(f"[prune] 用户插件根不存在，跳过: {plugins_dir}")
        return []
    if not builtin:
        print(
            "[prune] 找不到装机安装位（默认候选 + AGENTOS_INSTALL_DIR 均无）——"
            "无法判定影子，跳过（设 AGENTOS_INSTALL_DIR=<安装目录> 后重试）"
        )
        return []

    stale: list[tuple[str, str, str, Path]] = []  # (id, version, why, dir)
    orphans: list[str] = []
    for manifest_dir in _iter_plugin_manifests(plugins_dir, skip_modes=True):
        try:
            data = json.loads((manifest_dir / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        raw_id = data.get("id")
        raw_version = data.get("version")
        pid = raw_id if isinstance(raw_id, str) else manifest_dir.name
        version = raw_version if isinstance(raw_version, str) else None
        if pid not in builtin:
            failure = _import_smoke_failure(manifest_dir)
            if failure is not None:
                stale.append(
                    (
                        pid,
                        version or "",
                        f"内置已下架且 import 失败: {failure}",
                        manifest_dir,
                    )
                )
            else:
                orphans.append(f"{manifest_dir.name}（id={pid}）")
            continue
        builtin_version = builtin[pid][0]
        user_key = parse_semver_lenient(version) if version else None
        builtin_key = parse_semver_lenient(builtin_version) if builtin_version else None
        if user_key is None or builtin_key is None:
            print(f"  [skip] {manifest_dir.name}: 版本不可解析（用户 {version!r} / 内置 {builtin_version!r}）")
            continue
        if user_key <= builtin_key:
            stale.append((pid, version or "", f"版本 {version} <= 内置 {builtin_version}", manifest_dir))

    if orphans:
        print(f"[prune] 保留内置无同名插件 {len(orphans)} 个（用户自有）: {', '.join(orphans[:6])}")
    if not stale:
        print("[prune] 无过期影子副本或失效孤儿")
        return []
    print(f"[prune] 判死副本 {len(stale)} 个（影子旧版 / 孤儿 import 失败，BuiltinFirst 永不装载）:")
    for pid, version, why, manifest_dir in stale:
        print(f"  - {manifest_dir.name}（{pid} {version}）：{why}")
    if dry_run:
        print("[prune] [dry-run] 未移动")
        return []

    qdir = user_root / f"{QUARANTINE_PREFIX}{datetime.now().strftime('%Y%m%d')}" / "plugins"
    qdir.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    for pid, version, why, manifest_dir in stale:
        dst = qdir / manifest_dir.name
        if dst.exists():
            dst = qdir / f"{manifest_dir.name}__{len(moved)}"
        shutil.move(str(manifest_dir), str(dst))
        moved.append({"id": pid, "version": version, "dir": dst.name, "why": why})
    manifest_path = qdir.parent / "prune-manifest.json"
    try:
        prior = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else []
    except (OSError, ValueError):
        prior = []
    manifest_path.write_text(
        json.dumps(list(prior) + moved, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"[prune] 已隔离 {len(moved)} 个到 {qdir}（回滚=移回 <user_root>/plugins/）")
    return moved


def _iter_installed_venvs(plugins_dir: Path):
    """两层枚举装机插件 .venv（plugins/<id>/ 与 plugins/<组>/<id>/，如
    modes/mode_coding）；os.walk 剪枝 node_modules/.git（dsh_adapter 自嵌套
    junction 裸 glob 会挂死，同 bootstrap_plugin_envs 的定深教训）。"""
    if not plugins_dir.is_dir():
        return
    for root, dirs, _files in os.walk(plugins_dir):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
        if Path(root).name == ".venv" or ".venv" not in dirs:
            continue
        venv = Path(root) / ".venv"
        if (venv / "pyvenv.cfg").is_file():
            yield venv
        dirs.remove(".venv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="开发侧一键更新装机版用户空间（插件/配置/宿主addon），不动装机包",
    )
    parser.add_argument("--user-root", help="装机版用户空间根（必填才可写，如 %%APPDATA%%\\agentos）")
    parser.add_argument("--plugin", action="append", default=[], help="仓内插件目录（plugins/ 下相对路径），可多次")
    parser.add_argument("--config", action="append", default=[], help="config/ 下相对路径单文件，可多次")
    parser.add_argument(
        "--skills",
        nargs="?",
        const=".",
        default=None,
        help="skills/ 下相对子路径镜像到 <user_root>/skills/（缺省值 . = 整库），可与其他动作并用",
    )
    parser.add_argument("--addon", help="Godot 项目根（同步 hosts/godot-addons/agentos 到其 addons/）")
    parser.add_argument(
        "--heal-sdk",
        action="store_true",
        help="存量归一：走遍 <user_root>/plugins 全部插件 venv，SDK editable→实体"
        "（2026-10-29 装机 46 插件 editable 悬空清偿；可单独使用，需 --user-root）",
    )
    parser.add_argument(
        "--prune-stale",
        action="store_true",
        help="存量归一：隔离用户空间里版本 <= 内置副本的旧插件影子（BuiltinFirst 永不装载），"
        "移入 <user-root>/_quarantine_<日期>/ 可回滚；内置无同名 id 的用户自有插件不动",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列计划不落盘")
    args = parser.parse_args(argv)

    if not (args.plugin or args.config or args.addon or args.skills is not None or args.heal_sdk or args.prune_stale):
        parser.error(
            "至少给一个动作：--plugin / --config / --skills / --addon / --heal-sdk / --prune-stale"
            "（--dry-run 预览同样要先选动作）"
        )

    if args.addon and not (args.plugin or args.config or args.skills is not None):
        # addon 目标是外部 Godot 项目，与 user_root 无关——允许不给 --user-root
        sync_addon(args.addon, args.dry_run)
        return 0

    user_root = resolve_user_root(args.user_root)
    print(f"[target] 用户空间: {user_root}" + ("  [dry-run]" if args.dry_run else ""))
    if args.heal_sdk:
        healed = 0
        skipped = 0
        plugins_dir = user_root / "plugins"
        for venv in _iter_installed_venvs(plugins_dir):
            outcome = _heal_sdk_editable(venv, args.dry_run)
            if outcome:
                print(f"  [heal] {venv.parent.name}: {outcome}")
                healed += 1
            else:
                skipped += 1
        print(f"[heal-sdk] editable 归一 {healed} 个（无 editable 跳过 {skipped} 个）")
    ineffective: list[str] = []
    for rel in args.plugin:
        reason = sync_plugin(rel, user_root, args.dry_run)
        if reason:
            ineffective.append(reason)
    for rel in args.config:
        sync_config(rel, user_root, args.dry_run)
    if args.skills is not None:
        sync_skills(args.skills, user_root, args.dry_run)
    if args.addon:
        sync_addon(args.addon, args.dry_run)
    if args.prune_stale:
        prune_stale_plugins(user_root, dry_run=args.dry_run)
    # D4（审查总册 P1-15）：sync 汇总落「未生效 id 清单」——BuiltinFirst 下
    # 写坏版本号静默失效的显式告警面（装机侧结构化清单另见
    # GET /api/v1/plugins/source-arbitration）。
    if ineffective:
        print("[WARN] 未生效 id 清单（本轮 sync 这些插件不会生效）:")
        for reason in ineffective:
            print(f"  - {reason}")
    else:
        print("[OK] 未生效 id 清单: 空")
    print("[OK] 完成。内核重启或插件热发现后生效（配置覆盖需重启内核的步骤以装机版形态为准）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
