"""DSH 运行时工作区（设计 §4.2 C2：裁剪 JS 运行时的落位与状态面）。

工作区 = 用户空间的 **DSH profile**（package.json + ``dsh.profile.bundles``
+ cordis patch 文件 + pnpm 项目）——DSH 插件包是**用户数据**不是代码包
（装机/开发版运行时耦合禁令对齐）：install 即得，不与任何 DSH 发行仓耦合。
rc8 参考仓降级为桥协议回归测试夹具。

落位解析序（铁律）：显式配置 > ``AGENTOS_DSH_RUNTIME_ROOT`` > 用户空间默认
（``user_root()/dsh_runtime``；dev = ``<repo>/user_root/dsh_runtime/``，
装机 = ``%APPDATA%\\agentos\\dsh_runtime\\``，与内核 ``core::user_space`` 同源，
SDK 侧经 ``agentos_plugin_sdk.diagnostics_settings.user_root``）。本模块零
硬编码绝对路径（AC-R2）。

生命周期状态（§4.5 状态机）：profile 的 ``dsh.profile.bundles`` +
node_modules 是 installed/enabled 真值（官方 plugin-manager 写面）；本模块
的 ``.agentos/dsh-plugins-state.json`` 只承载审批/预览/错误等 UI 元数据。

boot 裁剪轴（AC-R4）：桥优先按 profile 的 bundle patch 组合官方 rows；本模块
的 ``boot_plan_for`` 保留为 composition 失败/旧工作区的回退扫描——空集
→ ``{chain: false}``（不加载任何 DSH 包）；有工具形包 → 最小链 + 包条目；
webServer/钩子轴为**声明位**（信号如实报告在 ``plan.signals``）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from npm_semver import satisfies

logger = logging.getLogger(__name__)

RUNTIME_ROOT_ENV = "AGENTOS_DSH_RUNTIME_ROOT"
WORKSPACE_DIR_NAME = "dsh_runtime"
STATE_DIR_NAME = ".agentos"
STATE_FILE_NAME = "dsh-plugins-state.json"

# boot 最小链的基础包（设计 §4.2 现状链的 npm 精确 pin；与 translator
# DSH_SOURCE_VERSION 同基准 0.2.1-rc.5；cordis 取稳定线）。工作区 bootstrap
# 一次性装入；桥从工作区 node_modules 解析（不再依赖任何仓库产物路径）。
# 0.2.1 契约漂移实证（2026-11-04 真机 boot）：dsh-tool-fs inject 链新增
# ``workingDirectory`` 服务 → @deepseek-ai/dsh-working-directory（其 inject
# 又需 ``sessionProjections`` → dsh-session-projection）；rc8 链无此依赖。
BOOT_PEER_PACKAGES: dict[str, str] = {
    "@deepseek-ai/cordis": "4.0.5",
    "@deepseek-ai/dsh-tools": "0.2.1-rc.5",
    "@deepseek-ai/dsh-system-prompt": "0.2.1-rc.5",
    "@deepseek-ai/dsh-fs-local": "0.2.1-rc.5",
    "@deepseek-ai/dsh-subprocess-local": "0.2.1-rc.5",
    "@deepseek-ai/dsh-session": "0.2.1-rc.5",
    "@deepseek-ai/dsh-session-projection": "0.2.1-rc.5",
    # dsh-system-prompt 的 peer（--legacy-peer-deps 装法不自动补 peer，须显式列入；
    # 2026-11-04 测试通道实测：插件安装后树重排致 boot 链缺 dsh-scope）
    "@deepseek-ai/dsh-scope": "0.2.1-rc.5",
    "@deepseek-ai/dsh-working-directory": "0.2.1-rc.5",
    "@deepseek-ai/dsh-tool-fs": "0.2.1-rc.5",
    "@deepseek-ai/dsh-tool-fs-search": "0.2.1-rc.5",
    # 社区 bundle 的常见 inject 目标：`webServer`（describe-image/pet 等）。
    # 实测 2026-11-09：挂上本包后 @linxin666/dsh-tool-describe-image 的
    # describe_image 才真正注册（inject = ["tools", "webServer"]）。
    "@deepseek-ai/dsh-host-webserver": "0.2.1-rc.5",
    # 宿主核心包：社区插件会直接 import 它却不声明 peer（describe-image 实测
    # `import { credentialRef } from '@deepseek-ai/dsh-credentials'`），官方宿主
    # 自带、最小宿主必须显式预装，否则 boot 时 import 失败。
    "@deepseek-ai/dsh-credentials": "0.2.1-rc.5",
}

# npm 纪律：精确版本 + 镜像 registry（本机网络纪律：GitHub 大传输禁走）
NPM_REGISTRY_DEFAULT = "https://registry.npmmirror.com"

# ── DSH profile 形态（官方 plugin-manager 的落位契约）──────────────
PROFILE_MANIFEST_NAME = "package.json"
PROFILE_CORDIS_NAME = "cordis.yml"
PROFILE_PATCH_NAME = "cordis.patch.yml"
PROFILE_PNPM_WORKSPACE_NAME = "pnpm-workspace.yaml"
PROFILE_ANCHOR_DIR = ".agentos/install-anchor"
PROFILE_HOME_DIR = ".dsh-home"
# pnpm 可执行独立落在 profile 外的运行时目录：pnpm 管理 profile 时不会
# prune/重装自己（自举鸡生蛋坑：@pnpm/exe 装进 profile 会被 pnpm 自身改动）。
PNPM_RUNTIME_DIR = ".agentos/pnpm-runtime"
MANAGER_PACKAGE = "@deepseek-ai/dsh-plugin-manager"
MANAGER_VERSION = "0.2.1-rc.5"
PNPM_PACKAGE = "@pnpm/exe"
PNPM_VERSION = "11.26.0"
NODE_MIN_VERSION = (22, 0, 0)

# 官方 plugin-manager 的运行时依赖/peer（0.2.1-rc.5 精确 pin；--legacy-peer-deps
# 装法不自动补 peer，须显式列入）。app-boot 的 include/group peer 取 ~1.0.x
# 满足区间，不能取 1.1.0。
MANAGER_PACKAGES: dict[str, str] = {
    "@deepseek-ai/cordis": "4.0.5",
    "@deepseek-ai/cordis-plugin-group": "1.0.5",
    "@deepseek-ai/cordis-plugin-include": "1.0.10",
    "@deepseek-ai/cordis-plugin-loader": "1.0.6",
    "@deepseek-ai/dsh-agent-preset-registry": "0.2.1-rc.5",
    "@deepseek-ai/dsh-app-boot": "0.2.1-rc.5",
    "@deepseek-ai/dsh-atomic-write": "0.2.1-rc.5",
    "@deepseek-ai/dsh-brand": "0.2.1-rc.5",
    "@deepseek-ai/dsh-home-paths": "0.2.1-rc.5",
    "@deepseek-ai/dsh-host-plugin-inventory": "0.2.1-rc.5",
    "@deepseek-ai/dsh-http-proxy": "0.2.1-rc.5",
    "@deepseek-ai/dsh-launch-environment": "0.2.1-rc.5",
    "@deepseek-ai/dsh-plugin-manager": MANAGER_VERSION,
    "@deepseek-ai/dsh-subprocess": "0.2.1-rc.5",
    "@deepseek-ai/dsh-tools": "0.2.1-rc.5",
    "@deepseek-ai/dsh-typert-protocol": "0.2.1-rc.5",
    "@deepseek-ai/dsh-util-values": "0.2.1-rc.5",
    "node-addon-require-builtin": "0.1.9",
}

# 宿主服务提供包（桥 provider 轴挂载的**具体实现**）：插件只 peer 声明接缝
# （dsh-credentials/dsh-attachment 等），实现包是宿主选择——不预置则
# credentials/connection/attachments/storage 服务缺席，依赖它们的 bundle 恒
# pending（dev 实测 2026-11-09：pet/sidecar/prompt-snippets 全挂起）。
PROVIDER_PACKAGES: dict[str, str] = {
    "@deepseek-ai/cordis-plugin-timer": "1.2.0",
    "@deepseek-ai/dsh-storage": "0.2.1-rc.5",
    "@deepseek-ai/dsh-storage-json": "0.2.1-rc.5",
    "@deepseek-ai/dsh-storage-domain": "0.2.1-rc.5",
    "@deepseek-ai/dsh-attachment-local": "0.2.1-rc.5",
    "@deepseek-ai/dsh-credentials-local": "0.2.1-rc.5",
    "@deepseek-ai/dsh-llm": "0.2.1-rc.5",
    "@deepseek-ai/dsh-llm-deepseek-api-key": "0.2.1-rc.5",
    "@deepseek-ai/dsh-commands": "0.2.1-rc.5",
    "@deepseek-ai/dsh-client-connection": "0.2.1-rc.5",
    "@deepseek-ai/dsh-agent-default-model": "0.2.1-rc.5",
}

# 工作区 bootstrap 全集：基础链 + 官方 manager 栈 + 宿主服务提供包
PROFILE_BOOTSTRAP_PACKAGES: dict[str, str] = {
    **BOOT_PEER_PACKAGES, **MANAGER_PACKAGES, **PROVIDER_PACKAGES,
}
# 管理页不展示的基础设施包（链/manager/pnpm）
PROFILE_INFRA_PACKAGES = frozenset({*PROFILE_BOOTSTRAP_PACKAGES, PNPM_PACKAGE})
# pnpm 独立运行时（不进 profile dependencies，避免 pnpm 自我 prune）
PNPM_BOOTSTRAP_PACKAGES: dict[str, str] = {PNPM_PACKAGE: PNPM_VERSION}

# pnpm 11 构建脚本审批白名单：基础栈的原生构建在 npm bootstrap 阶段已执行过
# （dsh-subprocess-local 经 koffi/node-pty 装原生绑定），此处显式放行——否则
# 每次 ``pnpm add`` 都以 ERR_PNPM_IGNORED_BUILDS 失败（实测 2026-11-09）。
# 用户 bundle 自带的构建脚本不在列，仍走 pnpm 待审批（pendingBuilds 诊断）。
PROFILE_ALLOWED_BUILDS: tuple[str, ...] = (
    "@deepseek-ai/dsh-subprocess-local",
    "koffi",
    "node-pty",
)

PROFILE_PNPM_WORKSPACE_TEMPLATE = "\n".join([
    "packages:",
    "  - .",
    "",
    "nodeLinker: hoisted",
    # 社区 bundle 把宿主包（dsh-credentials/dsh-commands/dsh-client-* 等）声明为
    # peer；本适配器只装最小链 + manager 栈，不预置全量宿主——peer 必须由 pnpm
    # 按 bundle 的声明补齐，否则装上了也在 boot 时 import 失败（实测 2026-11-09：
    # dsh-pet / dsh-cost-meter / dsh-tool-describe-image 全部缺 dsh-credentials）。
    "autoInstallPeers: true",
    f"registry: {NPM_REGISTRY_DEFAULT}",
    "allowBuilds:",
    *(f"  '{name}': true" for name in PROFILE_ALLOWED_BUILDS),
    "",
])


class WorkspaceBootstrapError(Exception):
    """工作区 bootstrap 失败（code 可判定，管理页按 code 展示原因）。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

# 静态信号正则（分类是静态信号，不执行 DSH 代码——translator.classify 同口径）
_TOOL_SIGNAL_RE = re.compile(r"tools\.register\s*\(|defineTool\s*\(")
_WEBSERVER_SIGNAL_RE = re.compile(r"webServer\s*\.\s*register\s*\(")
_HOOK_EVENT_RE = re.compile(r"ctx\.on\s*\(\s*['\"](agent/|tools/|session/)")

# webServer 轴的 provider 包：由轴自身装载（mjs ctx.plugin(WebServer)），
# 不再作为插件条目二次装载（双装载会重复注册路由/服务并抛错）。
WEB_SERVER_PROVIDER = "@deepseek-ai/dsh-host-webserver"


# ── 落位解析 ─────────────────────────────────────────────


def resolve_runtime_root(explicit: str | Path | None = None) -> Path:
    """工作区根：显式配置 > ``AGENTOS_DSH_RUNTIME_ROOT`` > 用户空间默认。"""
    if explicit:
        return Path(str(explicit))
    env_val = os.environ.get(RUNTIME_ROOT_ENV, "").strip()
    if env_val:
        return Path(env_val)
    from agentos_plugin_sdk.diagnostics_settings import user_root

    return user_root() / WORKSPACE_DIR_NAME


def ensure_workspace(root: str | Path) -> Path:
    """确保工作区是 DSH profile 形态（幂等；保留既有 dependencies/bundles）。

    profile 文件缺哪个补哪个；``package.json`` 只补齐缺失字段并迁移旧工作区
    里带 ``dsh.bundle`` 的已装依赖进 ``dsh.profile.bundles``——官方
    plugin-manager 以该清单为 enabled 真值。
    """
    ws = Path(root)
    ws.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(ws)
    changed = _ensure_manifest_fields(ws, manifest)
    if changed:
        _write_manifest(ws, manifest)
    _ensure_profile_files(ws)
    return ws


def _read_manifest(ws: Path) -> dict[str, Any]:
    pkg_json = ws / PROFILE_MANIFEST_NAME
    if not pkg_json.is_file():
        return {"name": "agentos-dsh-profile", "private": True}
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[dsh_workspace] package.json 解析失败，按新 profile 重建: %s | %s", pkg_json, exc)
        return {"name": "agentos-dsh-profile", "private": True}
    return data if isinstance(data, dict) else {"name": "agentos-dsh-profile", "private": True}


def _write_manifest(ws: Path, manifest: dict[str, Any]) -> None:
    (ws / PROFILE_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ensure_manifest_fields(ws: Path, manifest: dict[str, Any]) -> bool:
    """补齐 profile 必需字段；返回是否发生写入。"""
    changed = False
    if not manifest.get("name"):
        manifest["name"] = "agentos-dsh-profile"
        changed = True
    if manifest.get("private") is not True:
        manifest["private"] = True
        changed = True
    deps = manifest.get("dependencies")
    if not isinstance(deps, dict):
        manifest["dependencies"] = {}
        deps = manifest["dependencies"]
        changed = True
    if deps.pop(PNPM_PACKAGE, None) is not None:
        # pnpm 运行时是 profile 外的基础设施（.agentos/pnpm-runtime）；
        # 留在 dependencies 会让 pnpm 11 的 ignored-builds 拦截所有安装。
        changed = True
    dsh = manifest.get("dsh")
    if not isinstance(dsh, dict):
        dsh = {}
        manifest["dsh"] = dsh
        changed = True
    profile = dsh.get("profile")
    if not isinstance(profile, dict):
        profile = {}
        dsh["profile"] = profile
        changed = True
    bundles = profile.get("bundles")
    if not isinstance(bundles, list):
        profile["bundles"] = _migrate_bundles(ws, deps)
        changed = True
    return changed


def _migrate_bundles(ws: Path, dependencies: dict[str, Any]) -> list[str]:
    """旧工作区迁移：把已装且声明 ``dsh.bundle.patch`` 的依赖纳入 bundles。"""
    bundles: list[str] = []
    for name in dependencies:
        meta = read_package_json(ws / "node_modules" / name)
        dsh = (meta or {}).get("dsh")
        if isinstance(dsh, dict) and isinstance(dsh.get("bundle"), dict):
            bundles.append(name)
    return bundles


def _ensure_profile_files(ws: Path) -> None:
    """写 profile 必需的骨架文件（已存在绝不覆盖，保留用户/manager 编辑）。"""
    files = {
        PROFILE_CORDIS_NAME: "[]\n",
        PROFILE_PATCH_NAME: "[]\n",
        PROFILE_PNPM_WORKSPACE_NAME: PROFILE_PNPM_WORKSPACE_TEMPLATE,
    }
    for name, content in files.items():
        path = ws / name
        if not path.is_file():
            path.write_text(content, encoding="utf-8")
    anchor_dir = ws / PROFILE_ANCHOR_DIR
    anchor_dir.mkdir(parents=True, exist_ok=True)
    anchor = anchor_dir / PROFILE_MANIFEST_NAME
    if not anchor.is_file():
        anchor.write_text(json.dumps({
            "name": "agentos-dsh-install-anchor",
            "private": True,
            "dependencies": {},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ws / PROFILE_HOME_DIR).mkdir(parents=True, exist_ok=True)


def profile_manifest(root: str | Path) -> dict[str, Any]:
    """读 profile manifest（缺文件返回空 dict）。"""
    return read_package_json(Path(root)) or {}


def profile_bundles(root: str | Path) -> list[str]:
    """profile 当前选中的 bundle 清单（``dsh.profile.bundles``）。"""
    dsh = profile_manifest(root).get("dsh")
    if not isinstance(dsh, dict):
        return []
    profile = dsh.get("profile")
    if not isinstance(profile, dict):
        return []
    bundles = profile.get("bundles")
    return [str(name) for name in bundles] if isinstance(bundles, list) else []


def state_path(root: str | Path) -> Path:
    return Path(root) / STATE_DIR_NAME / STATE_FILE_NAME


def manager_installed(root: str | Path) -> bool:
    """profile 是否已 bootstrap 官方 plugin-manager（版本严格对齐 pin）。"""
    meta = read_package_json(Path(root) / "node_modules" / MANAGER_PACKAGE)
    return bool(meta and str(meta.get("version") or "") == MANAGER_VERSION)


def read_state(root: str | Path) -> dict[str, Any]:
    """读生命周期状态；缺文件/坏 JSON 回默认形态（状态缺失 ≠ 管理面崩）。"""
    p = state_path(root)
    if not p.is_file():
        return {"packages": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[dsh_workspace] 状态文件读取失败，回默认形态: %s | %s", p, exc)
        return {"packages": {}}
    if not isinstance(data, dict):
        return {"packages": {}}
    packages = data.get("packages")
    return {"packages": packages if isinstance(packages, dict) else {}}


def write_state(root: str | Path, state: dict[str, Any]) -> None:
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mark_package_state(root: str | Path, name: str, **fields: Any) -> dict[str, Any]:
    """合并单包状态字段（不丢既有键），返回全量状态。"""
    state = read_state(root)
    entry = state["packages"].get(name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(fields)
    state["packages"][name] = entry
    write_state(root, state)
    return state


def package_state(root: str | Path, name: str) -> dict[str, Any]:
    entry = read_state(root)["packages"].get(name)
    return dict(entry) if isinstance(entry, dict) else {}


def is_enabled(root: str | Path, name: str) -> bool:
    """默认启用；状态文件显式 ``enabled: false`` 才算禁用（§4.5：enable/disable 即生效）。"""
    entry = package_state(root, name)
    return bool(entry.get("enabled", True))


# ── node_modules 实扫（AC-M1：数据来自实扫而非写死清单） ─────────────


def read_package_json(pkg_dir: Path) -> dict[str, Any] | None:
    p = pkg_dir / "package.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[dsh_workspace] package.json 解析失败（跳过）: %s | %s", p, exc)
        return None
    return data if isinstance(data, dict) else None


def installed_packages(root: str | Path) -> list[dict[str, Any]]:
    """实扫 ``node_modules``（含 @scope 两层），返回 ``[{name, version, dir, package_json}]``。"""
    nm = Path(root) / "node_modules"
    if not nm.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(nm.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.name.startswith("@"):
            if not entry.is_dir():
                continue
            for sub in sorted(entry.iterdir()):
                if not sub.is_dir() or sub.name.startswith("."):
                    continue
                meta = read_package_json(sub)
                if meta:
                    out.append({
                        "name": meta.get("name") or f"{entry.name}/{sub.name}",
                        "version": str(meta.get("version") or ""),
                        "dir": str(sub),
                        "package_json": meta,
                    })
            continue
        if not entry.is_dir():
            continue
        meta = read_package_json(entry)
        if meta:
            out.append({
                "name": meta.get("name") or entry.name,
                "version": str(meta.get("version") or ""),
                "dir": str(entry),
                "package_json": meta,
            })
    return out


def dsh_engine_range(package_json: dict[str, Any]) -> str | None:
    """包的 ``engines.dsh`` 兼容声明；未声明 = 无约束（None）。"""
    engines = package_json.get("engines")
    if not isinstance(engines, dict):
        return None
    dsh = engines.get("dsh")
    return str(dsh) if isinstance(dsh, str) and dsh.strip() else None


def engine_compatible(package_json: dict[str, Any], host_version: str) -> bool | None:
    """``engines.dsh`` 兼容判定；未声明返回 None（npm 口径：缺声明 = 兼容）。"""
    rng = dsh_engine_range(package_json)
    if rng is None:
        return None
    try:
        return satisfies(host_version, rng)
    except ValueError:
        # 区间本身非法（生态脏数据）：fail-visible 由调用方按 False 拦截并报原因
        return False


def entry_path(pkg_dir: str | Path, package_json: dict[str, Any]) -> str | None:
    """包的桥装载入口（``lib/index.js`` 或 ``main``）；都无 = 非 apply 形包。"""
    base = Path(pkg_dir)
    candidate = base / "lib" / "index.js"
    if candidate.is_file():
        return str(candidate)
    main = package_json.get("main")
    if isinstance(main, str) and main.strip():
        cand = base / main
        if cand.is_file():
            return str(cand)
    return None


# ── boot 裁剪轴（AC-R4） ─────────────────────────────────────────


def _static_signals(pkg_dir: str | Path, entry: str | None) -> dict[str, bool]:
    """静态扫描包入口的贡献面信号（不执行代码；translator.classify 同口径）。"""
    if entry is None:
        return {"tool": False, "webServer": False, "hooks": False}
    try:
        blob = Path(entry).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"tool": False, "webServer": False, "hooks": False}
    return {
        "tool": bool(_TOOL_SIGNAL_RE.search(blob)),
        "webServer": bool(_WEBSERVER_SIGNAL_RE.search(blob)),
        "hooks": bool(_HOOK_EVENT_RE.search(blob)),
    }


def boot_plan_for(root: str | Path, host_dsh_version: str | None = None) -> dict[str, Any]:
    """按已启用包的贡献面并集生成 boot 计划（桥 initialize 的 ``bootPlan`` 参数）。

    - 空集（无包 / 只有基础包 / 全禁用）→ ``{chain: false, packages: []}``：
      空工作区 boot 不加载任何 DSH 包（AC-R4）；
    - 有包 → 最小工具链 + 各包入口条目；
    - 有包注册 webServer 路由（静态信号）→ ``webServer: true``（AC-X2 轴；
      provider=@deepseek-ai/dsh-host-webserver，缺包时桥侧 fail-visible，
      不拖垮工具面）；
    - 钩子轴信号如实报告在 ``signals.hooks``（拦截点由 checks 派发承载，
      不需要 boot 轴值）。
    """
    ws = Path(root)
    plan: dict[str, Any] = {"chain": False, "webServer": False, "hooks": False,
                            "packages": [], "signals": {"webServer": False, "hooks": False}}
    if not (ws / "node_modules").is_dir():
        return plan
    signals_agg = {"webServer": False, "hooks": False}
    entries: list[dict[str, Any]] = []
    for pkg in installed_packages(ws):
        name = pkg["name"]
        if name in PROFILE_INFRA_PACKAGES:
            continue  # 基础链/manager/pnpm 由各自装载面承载，不是插件条目
        if name == WEB_SERVER_PROVIDER:
            continue  # 轴 provider 由 webServer 轴装载（防双装载），不作插件条目
        if not is_enabled(ws, name):
            continue
        if host_dsh_version is not None:
            compat = engine_compatible(pkg["package_json"], host_dsh_version)
            if compat is False:
                continue  # engines.dsh 不兼容的已装包不进 boot（fail-visible 面）
        entry = entry_path(pkg["dir"], pkg["package_json"])
        sig = _static_signals(pkg["dir"], entry)
        signals_agg["webServer"] = signals_agg["webServer"] or sig["webServer"]
        signals_agg["hooks"] = signals_agg["hooks"] or sig["hooks"]
        if entry is None:
            continue  # 纯资产包（皮肤等）：不进桥装载
        entries.append({"name": name, "entry": entry})
    plan["signals"] = signals_agg
    if entries:
        plan["chain"] = True
        plan["packages"] = entries
    plan["webServer"] = signals_agg["webServer"]
    return plan


# ── npm/pnpm 命令构造与 bootstrap ─────────────────────────


def resolve_npm_binary() -> str:
    """解析可执行的 npm：Windows 上是 npm.cmd（CreateProcess 不能直调裸名，
    WinError 2 坑）；找不到返回裸名（让后续 spawn 以原生错误失败）。"""
    return shutil.which("npm") or "npm"


def resolve_pnpm_binary(root: str | Path | None = None) -> str | None:
    """解析工作区内置 pnpm：``.agentos/pnpm-runtime`` 优先，其次 profile。

    ``@pnpm/exe`` 的 setup 在 Windows 生成 ``pnpm.exe``（SEA 硬链接）；平台包
    ``@pnpm/win-x64/pnpm.exe`` 单独执行会找同目录 ``dist/pnpm.mjs``，不能直接
    当命令用。只认工作区内的真二进制（>1MB），不落到 PATH（bootstrap 负责
    把 pnpm 装进运行时目录）。
    """
    candidates: list[Path] = []
    if root is not None:
        ws = Path(root)
        runtime_nm = ws / PNPM_RUNTIME_DIR / "node_modules"
        nm = ws / "node_modules"
        candidates.extend([
            runtime_nm / "@pnpm" / "exe" / "pnpm.exe",
            runtime_nm / "@pnpm" / "exe" / "pnpm",
            nm / "@pnpm" / "exe" / "pnpm.exe",
            nm / "@pnpm" / "exe" / "pnpm",
        ])
    for candidate in candidates:
        if _valid_pnpm_binary(candidate):
            # 绝对路径：命令要跨进程交给 Node/pnpm，相对路径在 profile cwd
            # 下解析必失败（实测 2026-11-09：相对 root → pnpm.exe 找不到）。
            return str(candidate.resolve())
    return None


def _valid_pnpm_binary(path: Path) -> bool:
    """真二进制判定：``@pnpm/exe`` 的占位文件只有 15 字节，不能当命令用。"""
    try:
        return path.is_file() and path.stat().st_size > 1_000_000
    except OSError:
        return False


def resolve_pnpm_invocation(root: str | Path | None = None) -> tuple[str | None, list[str]]:
    """解析 pnpm 调用（command, args）；``@pnpm/exe`` 损坏时退 ``node dist/pnpm.mjs``。

    Returns:
        ``(command, args)``；未找到返回 ``(None, [])``。
    """
    pnpm = resolve_pnpm_binary(root)
    if pnpm is not None:
        return pnpm, []
    if root is not None:
        ws = Path(root)
        node = shutil.which("node")
        for base in (ws / PNPM_RUNTIME_DIR / "node_modules", ws / "node_modules"):
            dist = base / "@pnpm" / "exe" / "dist" / "pnpm.mjs"
            if dist.is_file() and node:
                return node, [str(dist.resolve())]
    return None, []


def node_version() -> tuple[int, int, int] | None:
    """当前 Node 版本（缺失/不可解析返回 None）。"""
    try:
        proc = subprocess.run(
            [shutil.which("node") or "node", "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = (proc.stdout or "").strip().lstrip("v").split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def bootstrap_command(workspace: str | Path,
                      registry: str = NPM_REGISTRY_DEFAULT) -> list[str]:
    """首次 bootstrap 命令：npm 装 profile 全集（官方 manager 栈 + 基础链）。

    不传 ``--legacy-peer-deps``：基础链包（如 dsh-tools）的 peer 是导入期硬依赖，
    必须由 npm 自动补齐（实测缺 ``@deepseek-ai/dsh-sandbox`` 时 boot 链直接
    导入失败）；精确版本仍由 ``profile_bootstrap_specs`` 固定。
    ``--package-lock=false`` 避免与 pnpm 的 lockfile 混用。
    """
    return [
        "npm", "install", "--prefix", str(workspace),
        "--registry", registry, "--no-audit", "--no-fund",
        "--save-exact", "--package-lock=false",
        *profile_bootstrap_specs(),
    ]


def pnpm_bootstrap_command(workspace: str | Path,
                           registry: str = NPM_REGISTRY_DEFAULT) -> list[str]:
    """pnpm 独立运行时安装命令（落 ``.agentos/pnpm-runtime``，不进 profile）。"""
    runtime_dir = Path(workspace) / PNPM_RUNTIME_DIR
    specs = [f"{name}@{ver}" for name, ver in sorted(PNPM_BOOTSTRAP_PACKAGES.items())]
    return [
        "npm", "install", "--prefix", str(runtime_dir),
        "--registry", registry, "--no-audit", "--no-fund",
        "--no-save", "--package-lock=false", *specs,
    ]


def pnpm_normalize_command(workspace: str | Path, pnpm: str,
                           pnpm_args: list[str] | None = None,
                           registry: str = NPM_REGISTRY_DEFAULT) -> list[str]:
    """npm 装的树归一成 pnpm 布局（首装前跑一次，避免运行中 DLL 被重写）。"""
    return [
        pnpm, *(pnpm_args or []), "--dir", str(Path(workspace).resolve()), "install",
        "--config.confirmModulesPurge=false", f"--registry={registry}",
    ]


def _default_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """真实执行（npm 命令换平台可执行形态；pnpm 已是绝对路径原样执行）。"""
    real = [resolve_npm_binary(), *cmd[1:]] if cmd and cmd[0] == "npm" else cmd
    try:
        proc = subprocess.run(real, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def ensure_bootstrapped(root: str | Path, runner: Any = None,
                        registry: str = NPM_REGISTRY_DEFAULT,
                        timeout_s: float = 900.0) -> dict[str, Any]:
    """确保 profile 工作区就绪（Node/npm 预检 + manager/pnpm 依赖）。

    Returns:
        ``{ready: True, bootstrapped: bool, pnpm: str, node: str}``。
    Raises:
        WorkspaceBootstrapError: node_missing/node_too_old/npm_missing/
            bootstrap_failed（code 可判定，管理页原样展示）。
    """
    ws = ensure_workspace(root)
    version = node_version()
    if version is None:
        raise WorkspaceBootstrapError(
            "node_missing", "未找到 Node（DSH 运行时要求 Node >= 22）")
    node_text = ".".join(str(part) for part in version)
    if version < NODE_MIN_VERSION:
        raise WorkspaceBootstrapError(
            "node_too_old",
            f"Node {node_text} 过低（DSH 运行时要求 Node >= 22）",
            node=node_text, required=">=22")
    manager_ready = manager_installed(ws)
    pnpm, pnpm_args = resolve_pnpm_invocation(ws)
    if manager_ready and pnpm and (ws / "node_modules" / ".modules.yaml").is_file():
        return {"ready": True, "bootstrapped": False, "pnpm": pnpm,
                "pnpm_args": pnpm_args, "node": node_text}
    if not shutil.which("npm"):
        raise WorkspaceBootstrapError(
            "npm_missing", "未找到 npm（首次 bootstrap 需要 npm）")
    execute = runner or _default_runner
    bootstrapped = False
    if not manager_ready:
        rc, out, err = execute(bootstrap_command(ws, registry), timeout_s)
        if rc != 0:
            tail = (err or out or "").strip()[-400:]
            raise WorkspaceBootstrapError(
                "bootstrap_failed", f"运行时 bootstrap 失败: {tail}", tail=tail)
        bootstrapped = True
    pnpm, pnpm_args = resolve_pnpm_invocation(ws)
    if not pnpm:
        rc, out, err = execute(pnpm_bootstrap_command(ws, registry), timeout_s)
        if rc != 0:
            tail = (err or out or "").strip()[-400:]
            raise WorkspaceBootstrapError(
                "bootstrap_failed", f"pnpm bootstrap 失败: {tail}", tail=tail)
        bootstrapped = True
        pnpm, pnpm_args = resolve_pnpm_invocation(ws)
    if not pnpm:
        raise WorkspaceBootstrapError(
            "bootstrap_failed", "bootstrap 完成但未找到 @pnpm/exe 可执行文件")
    if not (ws / "node_modules" / ".modules.yaml").is_file():
        # npm 装的树须先归一成 pnpm 布局：否则首次 pnpm add 会在转换中重写
        # 原生包目录，而运行中的桥进程持有其 DLL → ERR_PNPM_EPERM（实测 2026-11-09）。
        rc, out, err = execute(pnpm_normalize_command(ws, pnpm, pnpm_args, registry), timeout_s)
        if rc != 0:
            tail = (err or out or "").strip()[-400:]
            raise WorkspaceBootstrapError(
                "bootstrap_failed", f"pnpm 树归一失败: {tail}", tail=tail)
        bootstrapped = True
    return {"ready": True, "bootstrapped": bootstrapped, "pnpm": pnpm,
            "pnpm_args": pnpm_args, "node": node_text}


def npm_install_command(workspace: str | Path, specs: list[str],
                        registry: str = NPM_REGISTRY_DEFAULT,
                        legacy_peers: bool = False) -> list[str]:
    """工作区安装命令（精确版本由调用方拼进 specs；镜像 registry 固定带上）。

    ``legacy_peers``：0.1-rc 时代社区包 peer 声明与 0.2.1 工作区冲突
    （npm ERESOLVE 实证）时的装法开关——flag 插在 specs 前，保持
    「末位=spec」的调用方口径。

    首元素为裸 ``npm``（可移植契约）；执行方先经 ``resolve_npm_binary()``
    换成平台可执行形态（Windows npm.cmd）。
    """
    flags = ["--legacy-peer-deps"] if legacy_peers else []
    return [
        "npm", "install", "--prefix", str(workspace),
        "--registry", registry, "--no-audit", "--no-fund",
        *flags, *specs,
    ]


def npm_repair_command(workspace: str | Path,
                       registry: str = NPM_REGISTRY_DEFAULT) -> list[str]:
    """树修复命令（``npm install --force`` 无新 spec）：补 --legacy-peer-deps
    装法漏装的 peer（生态实证：该装法不自动补 peer）。"""
    return [
        "npm", "install", "--prefix", str(workspace),
        "--registry", registry, "--no-audit", "--no-fund", "--force",
    ]


def npm_uninstall_command(workspace: str | Path, names: list[str],
                          registry: str = NPM_REGISTRY_DEFAULT) -> list[str]:
    return [
        "npm", "uninstall", "--prefix", str(workspace),
        "--registry", registry, "--no-audit", "--no-fund",
        *names,
    ]


def bootstrap_spec() -> list[str]:
    """基础链精确 pin spec（桥协议回归测试的最小 bootstrap 集）。"""
    return [f"{name}@{ver}" for name, ver in sorted(BOOT_PEER_PACKAGES.items())]


def profile_bootstrap_specs() -> list[str]:
    """profile bootstrap 全集 spec：基础链 + 官方 manager 栈 + pnpm。"""
    return [f"{name}@{ver}" for name, ver in sorted(PROFILE_BOOTSTRAP_PACKAGES.items())]


# 内核注入的 llm.yaml 配置读面（server 装载期接入；本模块不直读 config/）。
_llm_config_provider: Callable[[], Any] | None = None


def set_llm_config_provider(provider: Callable[[], Any] | None) -> None:
    """接入内核按 ``config_files[id=llm]`` 注入的配置视图（None = 未接入）。"""
    global _llm_config_provider
    _llm_config_provider = provider


def default_agent_model(config: dict[str, Any] | None = None) -> dict[str, str] | None:
    """灵汐 LLM 默认模型 → DSH ``agentDefaultModel`` 服务配置（功能等价翻译）。

    ``config`` = 内核注入的 llm.yaml 内容（用户空间叠加后的生效视图，经
    manifest ``config_files[id=llm]`` 声明）；缺省取装载期接入的 provider。
    读不到/字段缺失返回 None（桥侧跳过 agentDefaultModel 提供包，缺口进
    providerStates fail-visible）。
    """
    data: Any = config
    if data is None and _llm_config_provider is not None:
        try:
            data = _llm_config_provider()
        except Exception:
            logger.warning(
                "[dsh_adapter] LLM 注入配置读取失败，跳过 agentDefaultModel",
                exc_info=True,
            )
            return None
    if not isinstance(data, dict):
        return None
    defaults = data.get("defaults")
    models = data.get("models")
    if not isinstance(defaults, dict) or not isinstance(models, dict):
        return None
    model_id = defaults.get("chat")
    entry = models.get(model_id) if isinstance(model_id, str) else None
    if not isinstance(entry, dict):
        return None
    provider = entry.get("provider")
    model_name = entry.get("model_name")
    if not isinstance(provider, str) or not provider:
        return None
    if not isinstance(model_name, str) or not model_name:
        return None
    return {"provider": provider, "model": model_name}


def profile_boot_plan(root: str | Path, host_dsh_version: str | None = None) -> dict[str, Any]:
    """桥 initialize 的 profile 计划：官方 composition 优先，旧扫描作回退。

    工具链只在有 bundle 时 boot；已 bootstrap 的 profile 以 ``dsh.profile.bundles``
    为唯一真值（node_modules 里是 manager 栈/peer，扫描会把它们误当插件条目）。
    未 bootstrap 的旧工作区才走扫描回退。空 profile 只挂 management context，
    initialize 返回空工具清单（AC-R4 的空集语义保留）。
    """
    fallback = boot_plan_for(root, host_dsh_version)
    if bool(profile_bundles(root)):
        chain = True
    elif manager_installed(root):
        chain = False  # 已 bootstrap：node_modules 里是 manager 栈/peer，扫描结果不可当真
    else:
        chain = bool(fallback.get("chain"))
    return {
        "source": "profile",
        "chain": chain,
        # 有 bundle 即挂 webServer 轴：社区 bundle 的 inject 常含 webServer
        # （describe-image/pet），缺该服务时插件能加载但工具不注册。
        "webServer": chain,
        # 本地服务翻译：灵汐模型配置 → DSH agentDefaultModel 服务配置
        # （功能等价时用灵汐配置背书，不要求用户重配一遍）。
        "agentDefaultModel": default_agent_model(),
        "fallback": fallback,
    }
