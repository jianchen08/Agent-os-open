"""DSH npm 运行时工作区（设计 §4.2 C2：裁剪 JS 运行时的落位与状态面）。

工作区 = 用户空间的独立 npm 项目（package.json + node_modules）——DSH 插件包
是**用户数据**不是代码包（装机/开发版运行时耦合禁令对齐）：install 即得，
不与任何 DSH 发行仓耦合。rc8 参考仓降级为桥协议回归测试夹具。

落位解析序（铁律）：显式配置 > ``AGENTOS_DSH_RUNTIME_ROOT`` > 用户空间默认
（``user_root()/dsh_runtime``；dev = ``<repo>/user_root/dsh_runtime/``，
装机 = ``%APPDATA%\\agentos\\dsh_runtime\\``，与内核 ``core::user_space`` 同源，
SDK 侧经 ``agentos_plugin_sdk.diagnostics_settings.user_root``）。本模块零
硬编码绝对路径（AC-R2）。

生命周期状态（§4.5 状态机）：node_modules 只承载 installed 集；enabled/
failed/error/requested_version 等状态落 ``<workspace>/.agentos/
dsh-plugins-state.json``（npm install 不触碰的用户数据区）。

boot 裁剪轴（AC-R4）：``boot_plan_for`` 按已启用包的贡献面并集生成——空集
→ ``{chain: false}``（不加载任何 DSH 包，含 cordis/dsh-tools 基础包）；
有工具形包 → 最小链 + 包条目；webServer/钩子轴本轮为**声明位**（信号如实
报告在 ``plan.signals``，引擎 P1 落地，AC-X1/X3）。
"""

from __future__ import annotations

import json
import logging
import os
import re
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
}

# npm 纪律：精确版本 + 镜像 registry（本机网络纪律：GitHub 大传输禁走）
NPM_REGISTRY_DEFAULT = "https://registry.npmmirror.com"

# 静态信号正则（分类是静态信号，不执行 DSH 代码——translator.classify 同口径）
_TOOL_SIGNAL_RE = re.compile(r"tools\.register\s*\(|defineTool\s*\(")
_WEBSERVER_SIGNAL_RE = re.compile(r"webServer\s*\.\s*register\s*\(")
_HOOK_EVENT_RE = re.compile(r"ctx\.on\s*\(\s*['\"](agent/|tools/|session/)")

# 供 boot 计划剔除的基础包名集合
_PEER_NAMES = frozenset(BOOT_PEER_PACKAGES)

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
    """确保工作区是 npm 项目形态（幂等；已有 package.json 绝不覆盖）。"""
    ws = Path(root)
    ws.mkdir(parents=True, exist_ok=True)
    pkg_json = ws / "package.json"
    if not pkg_json.is_file():
        pkg_json.write_text(
            json.dumps({
                "name": "dsh-runtime-workspace",
                "private": True,
                "description": "Lingxi AgentOS DSH plugin runtime workspace (user data)",
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return ws


def state_path(root: str | Path) -> Path:
    return Path(root) / STATE_DIR_NAME / STATE_FILE_NAME


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
        if name in _PEER_NAMES:
            continue  # 基础包由链自身承载，不是插件条目
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


# ── npm 命令构造 ─────────────────────────────────────────


def resolve_npm_binary() -> str:
    """解析可执行的 npm：Windows 上是 npm.cmd（CreateProcess 不能直调裸名，
    WinError 2 坑）；找不到返回裸名（让后续 spawn 以原生错误失败）。"""
    import shutil

    return shutil.which("npm") or "npm"


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
    """基础包精确 pin spec（工作区 bootstrap 一次装入）。"""
    return [f"{name}@{ver}" for name, ver in sorted(BOOT_PEER_PACKAGES.items())]
