"""DSH 包管理器（设计 §4.1 C1 管理服务面：list/install/uninstall/enable/disable）。

2026-11-09 起安装/卸载/启停委托官方 ``@deepseek-ai/dsh-plugin-manager``：
运行时工作区是 DSH profile，官方 manager 负责 pnpm 安装、profile manifest/
lockfile 回滚、取消、bundle 选择；本模块保留审批卡（第三方代码进执行面）、
翻译预览、皮肤同步与发现源。官方 manager 无 HMR 时变更返回
``restart-required``，本模块随后重启桥，下次工具调用按新 profile boot。

本地安装 = 官方 inspect/installBundle 原生能力（绝对目录/``file:``/tarball/
git）；只接受声明 ``dsh.bundle.patch`` 的 bundle 包（官方语义）。

install 安全档（设计 §九.1 用户裁定面）：第三方包 = 第三方代码装即进执行面
→ 走 **human-interaction capability 审批**（create_choice + wait_for_choice
既有面复用；端点返回 pending + 背景任务由用户裁决续行）；审批卡内容 =
**翻译预览**（装什么注册什么）。审批通道不可用 = 结构化错误 fail-visible；
deny / 超时 = 拒绝（超时=拒绝不放行）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import market_index as mi
import runtime_workspace as rw
from translator import scan_host_imports, translate_package, unmapped_slots

logger = logging.getLogger(__name__)

# 静态工具名提取（defineTool 形态最佳努力；权威工具面 = 桥 initialize 自省）
_TOOL_NAME_RE = re.compile(r"defineTool\s*\(\s*\{\s*name:\s*['\"]([\w.-]+)['\"]")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _empty_contribution() -> dict[str, Any]:
    """翻译预览空形（无包目录/翻译失败时的同构占位）。"""
    return {"tools": False, "tool_names": [], "themes": 0, "pages": 0, "hooks": 0,
            "slots": [], "unmapped": [], "host_dependencies": {"required": [], "missing": []}}


NpmRunner = Callable[[list[str], float], tuple[int, str, str]]
Fetcher = Callable[[str, float], Any]


class ManagerError(Exception):
    """结构化管理错误（code 可判定，管理页按 code 展示失败原因——AC-M2）。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.detail}


def mark_package_state(root: str | Path, name: str, **fields: Any) -> dict[str, Any]:
    """转发 runtime_workspace 同名函数（server/测试单点导入面）。"""
    return rw.mark_package_state(root, name, **fields)


def package_state(root: str | Path, name: str) -> dict[str, Any]:
    return rw.package_state(root, name)


def _default_npm_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """真实命令执行：仅 ``npm`` 首元素换平台可执行形态（WinError 2 坑见模块注释）。

    pnpm 归一命令的首元素是工作区 pnpm 绝对路径，必须原样执行——统一换 npm
    会让 ``--dir`` 变成 npm 未知参数（首装 bootstrap 必败，dev 实测 2026-11-09）。
    """
    real = [rw.resolve_npm_binary(), *cmd[1:]] if cmd and cmd[0] == "npm" else list(cmd)
    proc = subprocess.run(real, check=False, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def _extract_tarball(tarball: Path, dest: Path) -> Path:
    """npm tarball → 解包目录（返回包根 package/）。"""
    with tarfile.open(tarball, mode="r:gz") as tf:
        tf.extractall(dest, filter="data")  # noqa: S202 — 固定 npm registry 产物 + filter=data 防路径穿越
    return dest / "package"


class DshPackageManager:
    """DSH 生态包生命周期管理（dsh_adapter 管理端点的业务面）。"""

    def __init__(
        self,
        runtime_root: str | None = None,
        skins_dir: str | None = None,
        config: dict[str, Any] | None = None,
        npm_runner: NpmRunner | None = None,
        capability_factory: Callable[[], Any | None] | None = None,
        fetcher: Any = None,
        npm_timeout_s: float = 600.0,
        approval_timeout_s: float = 86400.0,
        bridge_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._explicit_root = runtime_root
        self._config = config or {}
        self._skins_dir = skins_dir
        self._npm_runner = npm_runner or _default_npm_runner
        self._capability_factory = capability_factory
        self._fetcher = fetcher
        self._npm_timeout_s = npm_timeout_s
        self._approval_timeout_s = approval_timeout_s
        self._bridge_factory = bridge_factory
        self._npm_tasks: dict[str, asyncio.Task[None]] = {}
        self._approval_tasks: dict[str, asyncio.Task[None]] = {}
        self._approval_request_ids: dict[str, tuple[str, str]] = {}
        self._cancelled: set[str] = set()
        self._index_plugins: list[dict[str, Any]] | None = None
        self._index_meta: dict[str, Any] = {}

    # ── 路径与资源 ───────────────────────────────────────

    @property
    def runtime_root(self) -> str:
        return str(rw.resolve_runtime_root(self._explicit_root))

    @property
    def skins_dir(self) -> str:
        if self._skins_dir:
            return str(self._skins_dir)
        from translator import SKIN_CENTER_SKINS_DIR

        return str(SKIN_CENTER_SKINS_DIR)

    def _workspace(self) -> Path:
        return rw.ensure_workspace(self.runtime_root)

    def _manager_section(self) -> dict[str, Any]:
        section = self._config.get("manager")
        return section if isinstance(section, dict) else {}

    def _registry(self) -> str:
        value = self._manager_section().get("registry")
        return str(value) if isinstance(value, str) and value else rw.NPM_REGISTRY_DEFAULT

    def _install_timeout_s(self) -> float:
        value = self._manager_section().get("install_timeout_s")
        try:
            return float(value) if value is not None else 900.0
        except (TypeError, ValueError):
            return 900.0

    def _manager_config(self) -> dict[str, Any]:
        section = self._manager_section()
        config: dict[str, Any] = {
            "registry": self._registry(),
            "fallbackRegistries": [],
            "outputBytes": 16384,
            "inspectTimeoutMs": int(section.get("inspect_timeout_ms", 20000)),
            "idleTimeoutMs": int(self._install_timeout_s() * 1000),
        }
        fallbacks = section.get("fallback_registries")
        if isinstance(fallbacks, list):
            config["fallbackRegistries"] = [str(url) for url in fallbacks if isinstance(url, str) and url]
        pnpm, pnpm_args = rw.resolve_pnpm_invocation(self.runtime_root)
        if pnpm:
            config["pnpmCommand"] = pnpm
            if pnpm_args:
                config["pnpmArgs"] = pnpm_args
        return config

    def _bridge(self) -> Any:
        if self._bridge_factory is not None:
            return self._bridge_factory()
        import bridge

        return bridge.get_bridge(runtime_root=self.runtime_root, manager_config=self._manager_config())

    def _manager_ready(self) -> bool:
        ws = Path(self.runtime_root)
        pnpm, _args = rw.resolve_pnpm_invocation(ws)
        return bool(rw.manager_installed(ws) and pnpm)

    async def _ensure_ready(self) -> dict[str, Any]:
        """bootstrap 工作区（Node/npm 预检 + manager/pnpm 依赖）。"""
        try:
            return await asyncio.to_thread(
                rw.ensure_bootstrapped, self.runtime_root, self._npm_runner, self._registry())
        except rw.WorkspaceBootstrapError as exc:
            raise ManagerError(exc.code, exc.message, **exc.detail) from exc

    async def _restart(self) -> None:
        """管理面变更后重启桥（restart-required 的下一次 boot 生效）。"""
        if self._bridge_factory is not None:
            return
        import bridge

        await bridge.restart_bridge()

    def set_index_plugins(self, plugins: list[dict[str, Any]]) -> None:
        """注入索引条目（管理端点的 GET index 先行抓取后转交；单测直注）。"""
        self._index_plugins = plugins

    async def _refresh_index(self) -> list[dict[str, Any]]:
        cache_dir = Path(self.runtime_root) / rw.STATE_DIR_NAME / "index-cache"
        out = await mi.fetch_market_index(
            sources=mi.sources_from_config(self._config),
            cache_dir=cache_dir, fetcher=self._fetcher,
        )
        self._index_plugins = out["plugins"]
        self._index_meta = {"sources": out["sources"], "cached": out["cached"], "stale": out["stale"]}
        return out["plugins"]

    async def _run_npm(self, cmd: list[str]) -> tuple[int, str, str]:
        """npm 子进程执行（线程池，不阻塞事件循环；timeout 兜底）。"""
        return await asyncio.to_thread(self._npm_runner, cmd, self._npm_timeout_s)

    # ── list（AC-M1：官方 manager listBundles + 翻译预览）──────────

    def _builtin_themes(self) -> list[dict[str, str]]:
        """适配器 manifest 内置主题（contributes.themes，走前端主题通道，非 DSH 包）。"""
        try:
            pj = json.loads((Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        themes = (pj.get("contributes") or {}).get("themes") or []
        return [
            {"id": t.get("id") or "", "name": t.get("name") or ""}
            for t in themes
            if isinstance(t, dict) and t.get("id")
        ]

    def _skin_providers(self, ws: Path) -> list[dict[str, Any]]:
        """皮肤提供方清单（2026-11-06 用户裁定：皮肤来自哪个插件，管理页就列哪个）。"""
        providers: list[dict[str, Any]] = []
        builtin = self._builtin_themes()
        if builtin:
            providers.append({"provider": "dsh_adapter", "source": "plugin_manifest",
                              "skins": builtin})
        claimed: set[str] = set()
        for pkg in rw.installed_packages(ws):
            ids = self._skin_ids_of(Path(pkg["dir"]))
            if ids:
                claimed.update(ids)
                providers.append({"provider": pkg["name"], "source": "dsh_package",
                                  "skins": [{"id": i, "name": i} for i in ids]})
        d = Path(self.skins_dir)
        orphan = sorted(p.name for p in d.iterdir()
                        if p.is_dir() and (p / "skin.json").is_file() and p.name not in claimed) if d.is_dir() else []
        if orphan:
            providers.append({"provider": "uninstalled-leftover", "source": "orphan",
                              "skins": [{"id": i, "name": i} for i in orphan]})
        return providers

    async def list_installed(self) -> dict[str, Any]:
        """已装清单：官方 listBundles 为真值 + 状态文件合并 + 逐包翻译预览摘要。"""
        ws = Path(self.runtime_root)
        rw.ensure_workspace(ws)
        state = rw.read_state(ws)
        # 读路径自愈：装机时 bootstrap 未就绪留下的「补装失败」陈旧记录，
        # 依赖实际已在位即清除（管理页不显示误导性失败；见对账方法）。
        self._reconcile_host_dep_errors(ws, state)
        skins = {"providers": self._skin_providers(ws), "skins_dir": self.skins_dir}
        if not self._manager_ready():
            pkgs = [p for p in rw.installed_packages(ws)
                    if p["name"] not in rw.PROFILE_INFRA_PACKAGES]
            out = [self._describe_package(ws, pkg, _state_entry(state, pkg["name"])) for pkg in pkgs]
            return {"initialized": False, "bootstrap_required": True, "workspace": str(ws),
                    "packages": out, "skins": skins}
        result = await self._bridge().manager_list()
        if not result.get("ok"):
            return {"initialized": True, "workspace": str(ws), "packages": [],
                    "skins": skins, "manager_error": str(result.get("error") or "manager unavailable")}
        data = result.get("data") or {}
        out = []
        for bundle in data.get("bundles") or []:
            if not isinstance(bundle, dict):
                continue
            name = str(bundle.get("name") or "")
            if not name or name in rw.PROFILE_INFRA_PACKAGES:
                continue
            if (bundle.get("availability") == "missing" and not bundle.get("installed")
                    and not bundle.get("enabled") and not bundle.get("error")):
                continue
            out.append(self._describe_bundle(ws, bundle, _state_entry(state, name)))
        # 非终态记录补进清单：官方 listBundles 只列已落盘 bundle，待审批/失败/
        # 取消记录若不在清单里，管理页就没有「待审批」行可裁决（用户实测：
        # 提交安装后页面只有旧清单、没有任何提示）。
        known = {str(row.get("name") or "") for row in out}
        for name, entry in (state.get("packages") or {}).items():
            if not isinstance(entry, dict) or name in known or name in rw.PROFILE_INFRA_PACKAGES:
                continue
            status = str(entry.get("status") or "")
            if status not in (
                "pending_approval", "failed", "approval_denied", "cancelled"
            ):
                continue
            entry2 = entry
            if (status == "pending_approval"
                    and str(entry.get("request_id") or "") not in self._approval_request_ids):
                # 读路径自愈：审批续行任务只在内存（_approval_request_ids），插件
                # 进程重启后该 pending 永远等不到裁决——落失败态并如实说明，
                # 别让管理页挂一条点「批准」也无效的待审批行。
                stale_error = "审批续行已随进程重启丢失，请重新发起安装"
                rw.mark_package_state(ws, name, status="failed", error=stale_error)
                entry2 = dict(entry, status="failed", error=stale_error)
            out.append(self._state_only_row(name, entry2))
        self._attach_activation(out)
        # 宿主服务提供包挂载态（fail-visible：缺哪个服务导致 bundle pending）
        providers = getattr(self._bridge(), "provider_states", []) or []
        return {"initialized": True, "workspace": str(ws), "packages": out, "skins": skins,
                "registries": data.get("registries"), "providers": providers}

    def _state_only_row(self, name: str, entry: dict[str, Any]) -> dict[str, Any]:
        """状态文件独有记录（待审批/失败/取消，bundle 尚未落盘）→ 管理页行。

        贡献面取安装时已算好的翻译预览（``preview``），字段形状与
        ``_describe_bundle`` 对齐——页面渲染路径不分叉。
        """
        preview_raw = entry.get("preview")
        preview: dict[str, Any] = preview_raw if isinstance(preview_raw, dict) else {}
        contribution = _empty_contribution()
        for key in ("tools", "themes", "hooks", "pages"):
            if key in preview:
                contribution[key] = preview[key]
        for key in ("tool_names", "slots", "unmapped"):
            if isinstance(preview.get(key), list):
                contribution[key] = preview[key]
        if isinstance(preview.get("host_dependencies"), dict):
            contribution["host_dependencies"] = preview["host_dependencies"]
        spec = str(entry.get("spec") or "")
        return {
            "name": name,
            "version": spec.rsplit("@", 1)[1] if "@" in spec and not spec.startswith("@") else "",
            "status": str(entry.get("status") or ""),
            "enabled": False,
            "installed": False,
            "engines_dsh": "",
            "engines_compatible": None,
            "contribution": contribution,
            "description": str(preview.get("description") or ""),
            "requested_version": entry.get("requested_version") or entry.get("version"),
            "error": entry.get("error"),
            "host_deps_error": entry.get("host_deps_error"),
            "request_id": entry.get("request_id"),
            "rows": [],
            "spec": spec,
        }

    def _attach_activation(self, rows: list[dict[str, Any]]) -> None:
        """合并桥侧 bundle 运行态（cordis Fiber 状态：pending=待注入服务）。"""
        states = getattr(self._bridge(), "bundle_states", []) or []
        by_key: dict[str, dict[str, Any]] = {}
        for entry in states:
            if not isinstance(entry, dict):
                continue
            for key in (entry.get("id"), entry.get("name")):
                if isinstance(key, str) and key:
                    by_key[key] = entry
        for row in rows:
            entry = by_key.get(str(row.get("name") or "")) or {}
            row["activation"] = str(entry.get("label") or "unknown")
            injects = entry.get("injects")
            row["activation_injects"] = injects if isinstance(injects, list) else []
            if entry.get("error"):
                row["activation_error"] = str(entry["error"])

    def _describe_package(self, ws: Path, pkg: dict[str, Any], entry_state: dict[str, Any]) -> dict[str, Any]:
        """旧工作区回退描述（manager 未 bootstrap 时的实扫形态）。"""
        pj = pkg["package_json"]
        enabled = bool(entry_state.get("enabled", True))
        status = entry_state.get("status") or ("enabled" if enabled else "disabled")
        return {
            "name": pkg["name"],
            "version": pkg["version"],
            "status": status,
            "enabled": enabled,
            "engines_dsh": rw.dsh_engine_range(pj),
            "engines_compatible": rw.engine_compatible(pj, self._host_version()),
            "contribution": self._contribution_preview(Path(pkg["dir"]), pj),
            "description": str(pj.get("description") or ""),
            "requested_version": entry_state.get("requested_version"),
            "error": entry_state.get("error"),
        }

    def _describe_bundle(self, ws: Path, bundle: dict[str, Any],
                         entry_state: dict[str, Any]) -> dict[str, Any]:
        """官方 BundleInfo + 状态文件 → 管理页行。"""
        name = str(bundle.get("name") or "")
        pkg_dir = ws / "node_modules" / name
        pj = rw.read_package_json(pkg_dir) or {}
        enabled = bool(bundle.get("enabled"))
        error = entry_state.get("error") or bundle.get("error")
        status = entry_state.get("status")
        if status in ("pending_approval", "failed", "approval_denied", "cancelled"):
            pass
        elif error:
            status = "failed"
        else:
            status = "enabled" if enabled else "disabled"
        contribution = (self._contribution_preview(pkg_dir, pj) if pkg_dir.is_dir()
                        else _empty_contribution())
        return {
            "name": name,
            "version": bundle.get("version") or pj.get("version") or "",
            "status": status,
            "enabled": enabled,
            "installed": bool(bundle.get("installed")),
            "official": bool(bundle.get("official")),
            "optional": bool(bundle.get("optional")),
            "removable": bool(bundle.get("removable")),
            "read_only_reason": bundle.get("readOnlyReason"),
            "engines_dsh": rw.dsh_engine_range(pj),
            "engines_compatible": rw.engine_compatible(pj, self._host_version()),
            "contribution": contribution,
            "description": str(bundle.get("description") or pj.get("description") or ""),
            "requested_version": entry_state.get("version") or entry_state.get("requested_version"),
            "error": error,
            "host_deps_error": entry_state.get("host_deps_error"),
            "application": entry_state.get("application"),
            "restart_required": bool(entry_state.get("restart_required")),
            "request_id": entry_state.get("request_id"),
            "rows": [str(row.get("moduleName") or "") for row in bundle.get("rows") or []
                     if isinstance(row, dict)],
            "overrides": list(bundle.get("overrides") or []),
            "source": bundle.get("source"),
            "spec": entry_state.get("spec"),
        }

    def _host_version(self) -> str:
        from translator import DSH_SOURCE_VERSION

        return DSH_SOURCE_VERSION

    def _contribution_preview(self, pkg_dir: Path, pj: dict[str, Any]) -> dict[str, Any]:
        """翻译预览摘要（M3）：工具形/主题数/挂载位/未映射/宿主依赖——静态 dry-run。"""
        out = _empty_contribution()
        try:
            manifest = translate_package(pkg_dir)
        except (FileNotFoundError, ValueError, OSError):
            return out
        kinds = manifest.get("kinds", {})
        out["tools"] = "tool" in kinds
        out["hooks"] = len(kinds.get("hook", {}).get("events", []))
        for candidate in (pkg_dir / "lib" / "index.js",):
            if candidate.is_file():
                try:
                    blob = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                out["tool_names"] = sorted(set(_TOOL_NAME_RE.findall(blob)))[:20]
        out["themes"] = len(self._skin_ids_of(pkg_dir))
        slots = manifest.get("client", {}).get("slots") or {}
        out["slots"] = [{"slot": name, "lingxi_slot": str(entry.get("lingxi_slot") or "direct")}
                        for name, entry in sorted(slots.items())]
        out["unmapped"] = unmapped_slots(slots)
        # 页面数 = 挂到页签/设置面板的槽（工作区页签 + 设置面板是灵汐的「页面」面）
        out["pages"] = sum(1 for item in out["slots"]
                           if item["lingxi_slot"] in ("workspaceTabs", "settingsPanels"))
        out["host_dependencies"] = self._host_dependencies(pkg_dir)
        return out

    def _host_dependencies(self, pkg_dir: Path) -> dict[str, list[str]]:
        """插件引用的宿主包与缺失项（未装且不在 bootstrap 预装清单）。"""
        required = scan_host_imports(pkg_dir)
        if not required:
            return {"required": [], "missing": []}
        installed = {p["name"] for p in rw.installed_packages(self.runtime_root)}
        missing = [name for name in required
                   if name not in installed and name not in rw.PROFILE_BOOTSTRAP_PACKAGES]
        return {"required": required, "missing": missing}

    def _skin_ids_of(self, pkg_dir: Path) -> list[str]:
        skins = pkg_dir / "skins"
        if not skins.is_dir():
            return []
        out = []
        for d in sorted(skins.iterdir()):
            if d.is_dir() and (d / "skin.json").is_file():
                out.append(d.name)
        return out

    # ── install（spec 归一化 + 官方 inspect + 审批 + 官方 installBundle）──

    async def install(self, spec: str, version: str | None = None,
                      legacy_peers: bool = False) -> dict[str, Any]:
        """安装 DSH bundle：官方 inspect → 翻译预览 → 审批 → 官方 installBundle。

        Args:
            spec: npm 包名 / 绝对目录 / ``file:`` / tarball / git 地址。
            version: registry 包名的精确版本；本地 spec 忽略。
            legacy_peers: 兼容旧调用面（官方 manager 自己处理 peer，不再使用）。

        Returns:
            ``{status: "pending_approval", request_id, preview}``——安装由背景
            任务续行，终态经 ``list_installed`` 可见（AC-R3 状态列）。
        """
        normalized = await self._normalize_spec(str(spec).strip(), version)
        await self._ensure_ready()
        bridge = self._bridge()
        inspected = await bridge.manager_inspect(normalized)
        if not inspected.get("ok"):
            raise ManagerError("manager_unavailable",
                               str(inspected.get("error") or "manager inspect failed"),
                               spec=normalized)
        data = inspected.get("data") or {}
        if data.get("status") == "refused":
            raise ManagerError(str(data.get("problem") or "invalid_spec"),
                               str(data.get("reason") or "spec refused"), spec=normalized)
        pkg_name = str(data.get("name") or normalized)
        preview = await self._build_preview(normalized, data)
        preview["manager"] = data
        preview["legacy_peers"] = bool(legacy_peers)
        return await self._approve_then_install(Path(self.runtime_root), pkg_name, normalized, preview)

    async def _normalize_spec(self, spec: str, version: str | None) -> str:
        """包名 → 精确版本；本地/tarball/git spec 原样（相对路径拒绝）。

        支持 npm 内联版本 ``name@1.2.3`` / ``@scope/name@1.2.3``（管理页与
        CLI 通用写法）——拆出显式版本后按精确版本安装，不再要求索引命中。
        """
        if not spec or spec.startswith("-"):
            raise ManagerError("invalid_request", f"invalid package spec: {spec!r}", spec=spec)
        if _is_git_spec(spec):
            return spec
        if _is_path_spec(spec):
            if _is_relative_path(spec):
                raise ManagerError("invalid_request", "本地路径必须是绝对路径", spec=spec)
            return spec
        name, inline_version = _split_name_version(spec)
        if not _valid_pkg_name(name):
            raise ManagerError("invalid_request", f"invalid package name: {spec!r}", spec=spec)
        if version:
            return f"{name}@{str(version).strip()}"
        resolved, _entry = await self._resolve_version(name, inline_version)
        return resolved

    async def _resolve_version(self, pkg: str, version: str | None) -> tuple[str, dict[str, Any] | None]:
        """显式版本 > 索引最新精确版本；不在索引且无显式版本 → 结构化错误。"""
        if version is not None:
            return f"{pkg}@{version}", None
        if self._index_plugins is None:
            try:
                await self._refresh_index()
            except (OSError, ValueError) as exc:
                logger.warning("[dsh_manager] 索引抓取失败（按不在索引处理）: %s", exc)
                self._index_plugins = []
        entry = mi.find_entry(self._index_plugins or [], pkg)
        if entry is None:
            raise ManagerError(
                "version_required_not_in_index",
                f"包 {pkg} 不在发现源索引且未给显式版本——禁 dist-tag（npm latest 陈旧实证），"
                "请提供精确版本",
                package=pkg,
            )
        latest = mi.latest_index_version(entry)
        if latest is None:
            raise ManagerError("version_required_not_in_index",
                               f"包 {pkg} 在索引中无可用版本", package=pkg)
        return f"{pkg}@{latest}", entry

    async def _build_preview(self, spec: str, inspection: dict[str, Any]) -> dict[str, Any]:
        """翻译预览（审批卡内容 / M3）：本地直接读，registry/git 走 npm pack。"""
        preview: dict[str, Any] = {
            "spec": spec,
            "method": "translator-dry-run",
            "manager_kind": inspection.get("kind"),
            "manager_bundle": inspection.get("bundle"),
        }
        try:
            with tempfile.TemporaryDirectory(prefix="dsh-preview-") as tmp:
                pkg_dir = await self._preview_dir(spec, inspection, Path(tmp))
                if pkg_dir is not None:
                    preview.update(self._contribution_preview(
                        pkg_dir, _read_json(pkg_dir / "package.json") or {}))
        except ManagerError as exc:
            preview["preview_error"] = exc.to_dict()
        return preview

    async def _preview_dir(self, spec: str, inspection: dict[str, Any],
                           tmp: Path) -> Path | None:
        kind = str(inspection.get("kind") or "")
        if kind == "path":
            local = Path(_strip_path_prefix(spec))
            if local.is_file():
                return _extract_tarball(local, tmp / "x")
            return local if local.is_dir() else None
        if kind == "tarball":
            local = Path(_strip_path_prefix(spec))
            return _extract_tarball(local, tmp / "x") if local.is_file() else None
        rc, _out, err = await self._run_npm(
            ["npm", "pack", spec, "--pack-destination", str(tmp),
             "--registry", rw.NPM_REGISTRY_DEFAULT, "--no-audit", "--no-fund"])
        if rc != 0:
            raise ManagerError("preview_failed",
                               f"npm pack 预览失败: {(err or '')[:200]}", spec=spec)
        tarballs = list(tmp.glob("*.tgz"))
        if not tarballs:
            raise ManagerError("preview_failed", f"npm pack 零产物: {(err or '')[:160]}", spec=spec)
        return _extract_tarball(tarballs[0], tmp / "x")

    async def _approve_then_install(self, ws: Path, key: str, spec: str,
                                    preview: dict[str, Any]) -> dict[str, Any]:
        """审批卡（翻译预览）→ 裁决 → 背景安装（pending 即同构挂起/恢复语义）。"""
        cap = self._capability()
        if cap is None:
            raise ManagerError(
                "approval_unavailable",
                "审批通道不可用（human-interaction capability 未注入）——安装被拒，"
                "第三方代码未经审批不进执行面",
                package=key,
            )
        try:
            created = await cap.call("create_choice", {
                # 安装审批不是会话内交互：human.create_choice 要求 session/thread/tab
                # 三键，给固定合成命名空间（前端 pending 列表不按会话过滤，卡片照常展示）
                "session_id": "dsh_adapter:install",
                "thread_id": "dsh_adapter:install",
                "tab_id": "dsh_adapter:install",
                "title": f"DSH 插件安装审批：{key}",
                "description": "翻译预览（装什么注册什么）：\n" + json.dumps(preview, ensure_ascii=False, indent=1),
                "options": [
                    {"id": "approve", "label": "批准安装", "semantics": "approve_once"},
                    {"id": "deny", "label": "拒绝", "semantics": "deny"},
                ],
                "timeout_seconds": int(self._approval_timeout_s),
            }, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 — capability 通道故障=审批不可用（fail-visible）
            raise ManagerError("approval_unavailable",
                               f"审批通道调用失败: {exc}", package=key) from exc
        request_id = str((created or {}).get("request_id") or "")
        if not request_id:
            raise ManagerError("approval_unavailable",
                               f"审批请求创建失败: {created!r}", package=key)
        self._approval_request_ids[request_id] = (key, spec)
        rw.mark_package_state(ws, key, status="pending_approval", spec=spec,
                              preview=preview, request_id=request_id)
        task = asyncio.create_task(self._finish_install(ws, key, spec, request_id))
        self._npm_tasks[key] = task
        self._approval_tasks[request_id] = task
        return {"status": "pending_approval", "request_id": request_id,
                "package": key, "spec": spec, "preview": preview}

    def _capability(self) -> Any | None:
        if self._capability_factory is None:
            return None
        try:
            return self._capability_factory()
        except (KeyError, AttributeError):
            return None

    async def _wait_choice(self, cap: Any, request_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """等待裁决；通道故障返回 ``(None, 故障原因)``，取消异常照常向上传播。"""
        try:
            waited = await cap.call("wait_for_choice", {"request_id": request_id},
                                    timeout=self._approval_timeout_s)
        except Exception as exc:  # noqa: BLE001 — 等待通道故障=拒绝不放行（fail-visible 留痕）
            return None, str(exc)
        return waited, None

    async def _finish_install(self, ws: Path, key: str, spec: str, request_id: str) -> None:
        """背景续行：等待裁决 → 批准则官方 installBundle + 皮肤同步 + 状态终态。"""
        cap = self._capability()
        if cap is None:
            rw.mark_package_state(ws, key, status="failed",
                                  error="approval channel unavailable at wait time")
            return
        try:
            waited, wait_error = await self._wait_choice(cap, request_id)
        except asyncio.CancelledError:
            return  # 取消路径已由 cancel_install 落状态
        if wait_error is not None:
            rw.mark_package_state(ws, key, status="failed",
                                  error=f"approval wait failed: {wait_error}")
            return
        if request_id in self._cancelled:
            return
        # human 服务实测形态是扁平信封（request_id/response_type/selected_option/
        # selected_semantics 顶层）；兼容旧包装形态（status+response 子对象）。
        waited_dict = waited or {}
        response = waited_dict.get("response") or waited_dict
        selected = response.get("selected_option")
        semantics = response.get("selected_semantics")
        status = waited_dict.get("status") or response.get("response_type")
        # 裁决读语义（ADR 2026-10-01 消费方只读语义）：human 服务把任何词形归一成
        # 卡上 approve_once 选项的规范 id（如 approved_once），不能按 id 反推。
        approved = status == "answered" and (
            semantics == "approve_once" or (semantics is None and selected == "approve"))
        if not approved:
            try:
                await cap.call("cancel", {"request_id": request_id}, timeout=10.0)
            except Exception:  # noqa: BLE001 — 取消是收尾尽力而为
                pass
            rw.mark_package_state(ws, key, status="approval_denied",
                                  error="安装被拒绝或超时（超时=拒绝不放行）")
            return
        bridge = self._bridge()
        # 真实桥走一次性隔离进程（先停桥再 pnpm，防 Windows 文件句柄 EPERM）；
        # 测试桩件无该方法时走原常驻 RPC 路径。
        isolated_install = getattr(bridge, "manager_install_isolated", None)
        if isolated_install is not None:
            result = await isolated_install(
                spec, request_id, {"saveExact": True}, timeout_s=self._install_timeout_s())
        else:
            result = await bridge.manager_install(
                spec, request_id, {"saveExact": True}, timeout_s=self._install_timeout_s())
        if request_id in self._cancelled:
            return
        if not result.get("ok"):
            rw.mark_package_state(ws, key, status="failed",
                                  error=str(result.get("error") or "manager install failed"))
            return
        data = result.get("data") or {}
        application = str(data.get("application") or "")
        if data.get("error") or application == "failed":
            rw.mark_package_state(ws, key, status="failed",
                                  error=_manager_error_text(data),
                                  diagnostics=data.get("packageResult"))
            return
        if application == "cancelled":
            rw.mark_package_state(ws, key, status="cancelled", error="安装已取消")
            return
        bundle = str(data.get("bundle") or key)
        await self._post_install(ws, bundle)
        await self._ensure_host_deps(ws, bundle)
        if bundle != key:
            state = rw.read_state(ws)
            state["packages"].pop(key, None)
            rw.write_state(ws, state)
        rw.mark_package_state(ws, bundle, status="installed", enabled=True, error=None,
                              spec=spec, version=data.get("version"),
                              application=application,
                              restart_required=application == "restart-required")
        await self._restart()

    # ── uninstall / enable / disable（官方 bundle 生命周期）─────────

    async def uninstall(self, pkg: str) -> dict[str, Any]:
        await self._ensure_ready()
        bridge = self._bridge()
        isolated_remove = getattr(bridge, "manager_remove_isolated", None)
        result = (await isolated_remove(pkg)) if isolated_remove is not None \
            else await bridge.manager_remove(pkg)
        if not result.get("ok"):
            raise ManagerError("manager_unavailable",
                               str(result.get("error") or "manager remove failed"), package=pkg)
        data = result.get("data") or {}
        if data.get("error") or data.get("application") == "failed":
            raise ManagerError(_manager_error_code(data, "remove_failed"),
                               _manager_error_text(data), package=pkg)
        ws = Path(self.runtime_root)
        entry = rw.package_state(ws, pkg)
        await asyncio.to_thread(self._drop_skins, entry.get("skins", []) or [])
        state = rw.read_state(ws)
        state["packages"].pop(pkg, None)
        rw.write_state(ws, state)
        await self._restart()
        return {"ok": True, "package": pkg, "application": data.get("application")}

    async def enable(self, pkg: str) -> dict[str, Any]:
        return await self._set_enabled(pkg, True)

    async def disable(self, pkg: str) -> dict[str, Any]:
        return await self._set_enabled(pkg, False)

    async def _set_enabled(self, pkg: str, enabled: bool) -> dict[str, Any]:
        await self._ensure_ready()
        result = await self._bridge().manager_set_enabled(pkg, enabled)
        if not result.get("ok"):
            raise ManagerError("manager_unavailable",
                               str(result.get("error") or "manager enable failed"), package=pkg)
        data = result.get("data") or {}
        if data.get("error") or data.get("application") == "failed":
            raise ManagerError(_manager_error_code(data, "enable_failed"),
                               _manager_error_text(data), package=pkg)
        ws = Path(self.runtime_root)
        entry = rw.package_state(ws, pkg)
        skins = entry.get("skins", []) or []
        if skins:
            await self._reconcile_skins(pkg, skins, enabled)
        rw.mark_package_state(ws, pkg, enabled=enabled,
                              status="enabled" if enabled else "disabled",
                              application=data.get("application"))
        await self._restart()
        return {"ok": True, "package": pkg, "enabled": enabled,
                "application": data.get("application")}

    async def _post_install(self, ws: Path, pkg: str) -> None:
        """装后翻译面落位：皮肤包资产同步进皮肤目录（主题选择器即现，M2）。"""
        for p in rw.installed_packages(ws):
            if p["name"] != pkg:
                continue
            # 皮肤目录复制体量与包同阶（含样式/资产），出事件循环
            skins = await asyncio.to_thread(self._install_skin_dirs, Path(p["dir"]))
            if skins:
                rw.mark_package_state(ws, pkg, skins=skins)
            break

    def _reconcile_host_dep_errors(self, ws: Path, state: dict[str, Any]) -> None:
        """宿主依赖错误记录对账（读路径自愈）。

        装机时序坑：安装先于 bridge bootstrap 完成时，``_ensure_host_deps``
        记下 ``profile context unavailable``——但依赖随后被 bootstrap/其他
        安装带入，陈旧失败记录会让管理页长期显示误导性红字。列包时按文件
        系统现算：缺失清零 → 清错误记录；仍缺 → 保留（fail-visible）。
        """
        for name, entry in list((state.get("packages") or {}).items()):
            if not isinstance(entry, dict) or not entry.get("host_deps_error"):
                continue
            pkg_dir = ws / "node_modules" / name
            if not pkg_dir.is_dir():
                continue
            if self._host_dependencies(pkg_dir)["missing"]:
                continue
            rw.mark_package_state(ws, name,
                                  host_dependencies=entry.get("host_dependencies") or [],
                                  host_deps_error=None)
            entry["host_deps_error"] = None

    async def _ensure_host_deps(self, ws: Path, pkg: str) -> None:
        """装后补装宿主依赖：扫描插件引用，缺的按宿主精确版本经官方
        ``runProfilePnpm`` 补装（不写 profile bundles）。

        失败不阻断安装（插件本身已装上），``host_deps_error`` 进状态文件
        fail-visible——管理页如实显示「依赖没补上」而不是假装可用。
        """
        pkg_dir = ws / "node_modules" / pkg
        if not pkg_dir.is_dir():
            return
        deps = self._host_dependencies(pkg_dir)
        if not deps["missing"]:
            rw.mark_package_state(ws, pkg, host_dependencies=deps["required"],
                                  host_deps_error=None)
            return
        specs = [f"{name}@{self._host_version()}" for name in deps["missing"]]
        try:
            result = await self._bridge().manager_add_host_deps(specs)
        except Exception as exc:  # noqa: BLE001 — 补装故障=插件可装但依赖缺（fail-visible）
            rw.mark_package_state(ws, pkg, host_dependencies=deps["required"],
                                  host_deps_error=f"host dep install failed: {exc}")
            return
        if not result.get("ok"):
            rw.mark_package_state(
                ws, pkg, host_dependencies=deps["required"],
                host_deps_error=str(result.get("error") or "host dep install failed"))
            return
        rw.mark_package_state(ws, pkg, host_dependencies=deps["required"],
                              host_deps_installed=deps["missing"], host_deps_error=None)

    @staticmethod
    def _refresh_skin_dir(src: Path, dest: Path) -> None:
        """单个皮肤目录刷新：旧副本先删（rmtree）再整树复制（copytree）。"""
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

    def _install_skin_dirs(self, pkg_dir: Path) -> list[str]:
        skins = self._skin_ids_of(pkg_dir)
        if not skins:
            return []
        dest_root = Path(self.skins_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        for skin in skins:
            self._refresh_skin_dir(pkg_dir / "skins" / skin, dest_root / skin)
        return skins

    def _drop_skins(self, skins: list[str]) -> None:
        """回收皮肤目录（缺目录不报错——启停/卸载幂等）。"""
        dest_root = Path(self.skins_dir)
        for skin in skins:
            shutil.rmtree(dest_root / skin, ignore_errors=True)

    def _reconcile_skins_sync(self, pkg: str, skins: list[str], enabled: bool) -> None:
        if enabled:
            for p in rw.installed_packages(Path(self.runtime_root)):
                if p["name"] != pkg:
                    continue
                for skin in skins:
                    src = Path(p["dir"]) / "skins" / skin
                    if src.is_dir():
                        self._refresh_skin_dir(src, Path(self.skins_dir) / skin)
                break
        else:
            self._drop_skins(skins)

    async def _reconcile_skins(self, pkg: str, skins: list[str], enabled: bool) -> None:
        """皮肤包启停 = 皮肤目录同步/回收（主题选择器即现/即消，M2 逆向）。"""
        # 皮肤目录复制/删除体量与包同阶，整体出事件循环（纯同步体+async 壳）
        await asyncio.to_thread(self._reconcile_skins_sync, pkg, skins, enabled)

    # ── 取消与后台收尾 ─────────────────────────────────────

    async def respond_install(self, request_id: str, approve: bool) -> dict[str, Any]:
        """管理页内联裁决（安装审批的同位替代面）。

        审批卡浮层走 WS interaction_request 事件，内核按 thread_id 单播——安装
        审批用合成命名空间 ``dsh_adapter:install``，无会话订阅该 thread，事件
        恒丢弃（用户实测：点安装后只有状态行、无任何提示）。管理页即审批面：
        行内「批准/拒绝」经 human-interaction ``respond`` 唤醒背景
        ``wait_for_choice``，与卡片裁决同一条续行链路。
        """
        if request_id not in self._approval_request_ids:
            raise ManagerError(
                "not_found",
                f"未知安装审批请求: {request_id}（内核重启后背景续行已失，请重新发起安装）",
                request_id=request_id,
            )
        cap = self._capability()
        if cap is None:
            raise ManagerError("approval_unavailable",
                               "审批通道不可用（human-interaction capability 未注入）",
                               request_id=request_id)
        try:
            out = await cap.call("respond", {
                "request_id": request_id,
                "response": {
                    "response_type": "answered",
                    "selected_option": "approve" if approve else "deny",
                },
            }, timeout=15.0)
        except Exception as exc:  # noqa: BLE001 — 通道故障=fail-visible
            raise ManagerError("approval_unavailable",
                               f"审批应答失败: {exc}", request_id=request_id) from exc
        return {"ok": True, "request_id": request_id,
                "approved": bool(approve), "channel": out}

    async def cancel_install(self, request_id: str) -> dict[str, Any]:
        """取消审批中的安装或运行中的 pnpm（官方 cancelInstall + 审批卡取消）。"""
        key_spec = self._approval_request_ids.get(request_id)
        if key_spec is None:
            raise ManagerError("not_found", f"未知安装请求: {request_id}", request_id=request_id)
        key, _spec = key_spec
        self._cancelled.add(request_id)
        result = await self._bridge().manager_cancel(request_id)
        cap = self._capability()
        if cap is not None:
            try:
                await cap.call("cancel", {"request_id": request_id}, timeout=10.0)
            except Exception:  # noqa: BLE001 — 取消是收尾尽力而为
                pass
        task = self._approval_tasks.get(request_id)
        if task is not None and not task.done():
            task.cancel()
        ws = Path(self.runtime_root)
        rw.mark_package_state(ws, key, status="cancelled", error="安装已取消")
        return {"ok": True, "request_id": request_id, "status": "cancelled",
                "manager": result.get("data") if result.get("ok") else None}

    async def wait_background(self, timeout_s: float = 120.0) -> list[str]:
        """等待全部背景安装任务收尾（测试收尾用；生产面不调）。"""
        if not self._npm_tasks:
            return []
        tasks = list(self._npm_tasks.values())
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
        return list(self._npm_tasks.keys())

    async def refresh_index(self) -> dict[str, Any]:
        """GET index 端点面：多源抓取 + 缓存降级态（设计 §4.1 发现源）。"""
        cache_dir = Path(self.runtime_root) / rw.STATE_DIR_NAME / "index-cache"
        out = await mi.fetch_market_index(
            sources=mi.sources_from_config(self._config),
            cache_dir=cache_dir, fetcher=self._fetcher,
        )
        self._index_plugins = out["plugins"]
        return out


def _state_entry(state: dict[str, Any], name: str) -> dict[str, Any]:
    entry = state.get("packages", {}).get(name)
    return dict(entry) if isinstance(entry, dict) else {}


def _manager_error_text(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    if error:
        return str(error)
    package_result = data.get("packageResult")
    if isinstance(package_result, dict) and package_result.get("output"):
        return str(package_result["output"])[-400:]
    return "官方 plugin-manager 操作失败"


def _manager_error_code(data: dict[str, Any], default: str) -> str:
    error = data.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return default


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _strip_path_prefix(spec: str) -> str:
    for prefix in ("file:", "link:"):
        if spec.startswith(prefix):
            return spec[len(prefix):]
    return spec


def _is_path_spec(spec: str) -> bool:
    """绝对/相对路径、file:/link:、tarball 形态。"""
    return (spec.startswith(("/", "\\", ".", "~", "file:", "link:"))
            or _DRIVE_PATH_RE.match(spec) is not None
            or spec.endswith((".tgz", ".tar.gz")))


def _is_relative_path(spec: str) -> bool:
    value = _strip_path_prefix(spec)
    if value.startswith(("/", "\\")) or _DRIVE_PATH_RE.match(value):
        return False
    return True


def _is_git_spec(spec: str) -> bool:
    return spec.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:",
                            "ssh://", "git@", "http://", "https://"))


def _split_name_version(spec: str) -> tuple[str, str | None]:
    """拆 npm 内联版本：``name@1.2.3`` / ``@scope/name@1.2.3`` → (name, version)。

    裸名（含 ``@scope/name`` 的单个前导 ``@``）原样返回版本 None。
    """
    at = spec.rfind("@")
    if at <= 0:
        return spec, None
    candidate = spec[at + 1:]
    if not candidate or any(ch in candidate for ch in ("/", "\\", " ", ":")):
        return spec, None
    return spec[:at], candidate


def _valid_pkg_name(pkg: str) -> bool:
    """npm 包名形态守门：裸名或 ``@scope/name``；拒路径/参数注入形态。"""
    if not pkg or pkg.startswith(("-", ".")):
        return False
    if any(ch in pkg for ch in (" ", "\\", ":", "..")):
        return False
    if len(pkg) > 200:
        return False
    if "/" in pkg:
        parts = pkg.split("/")
        if len(parts) != 2 or not parts[0].startswith("@") or not parts[0][1:] or not parts[1]:
            return False
    elif pkg.startswith("@"):
        return False
    return True
