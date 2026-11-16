"""DSH 包管理器（设计 §4.1 C1 管理服务面：list/install/uninstall/enable/disable）。

生命周期状态机（§4.5）：未安装 → installed →（翻译成功）enabled ⇄ disabled；
翻译失败 → failed（原因可见）。状态落运行时工作区
``.agentos/dsh-plugins-state.json``（node_modules 只承载 installed 集）——
端点与状态文件同源（AC-R3），enable/disable 即生效（下轮桥 boot 按
``boot_plan_for`` 重算裁剪轴，无"需重启"态）。

install 安全档（设计 §九.1 用户裁定面）：第三方 npm 包 = 第三方代码装即进
执行面 → 走 **human-interaction capability 审批**（create_choice +
wait_for_choice 既有面复用；sidecar HTTP handler 无 pipeline run 可 suspend，
"端点返回 pending + 背景任务由用户裁决续行"即同构的挂起/恢复语义），审批卡
内容 = **翻译预览**（装什么注册什么）。审批通道不可用 = 结构化错误
fail-visible（不静默绕过审批面）；deny / 超时 = 拒绝（超时=拒绝不放行）。

engines.dsh 兼容声明不匹配 → 安装前拦截（npm view 元数据查询，零下载，
审批与工作区写盘之前）；缺省版本 = 索引最新精确版本（禁 dist-tag）；
uninstall 有依赖者 → 拒绝并列依赖者。
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
from translator import translate_package

logger = logging.getLogger(__name__)

# 静态工具名提取（defineTool 形态最佳努力；权威工具面 = 桥 initialize 自省）
_TOOL_NAME_RE = re.compile(r"defineTool\s*\(\s*\{\s*name:\s*['\"]([\w.-]+)['\"]")

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
    """真实 npm 执行（resolve_npm_binary 换平台形态；WinError 2 坑见模块注释）。"""
    real = [rw.resolve_npm_binary(), *cmd[1:]]
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
    ) -> None:
        self._explicit_root = runtime_root
        self._config = config or {}
        self._skins_dir = skins_dir
        self._npm_runner = npm_runner or _default_npm_runner
        self._capability_factory = capability_factory
        self._fetcher = fetcher
        self._npm_timeout_s = npm_timeout_s
        self._approval_timeout_s = approval_timeout_s
        self._npm_tasks: dict[str, asyncio.Task[None]] = {}
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

    # ── list（AC-M1 实扫）────────────────────────────────

    async def list_installed(self) -> dict[str, Any]:
        """已装清单：node_modules 实扫 + 状态文件合并 + 逐包翻译预览摘要。"""
        ws = Path(self.runtime_root)
        pkgs = rw.installed_packages(ws)
        state = rw.read_state(ws)
        if not pkgs:
            return {"initialized": False, "workspace": str(ws), "packages": []}
        out: list[dict[str, Any]] = []
        for pkg in pkgs:
            name = pkg["name"]
            if name in rw.BOOT_PEER_PACKAGES:
                continue  # 基础包是链自身，不是插件条目
            entry_state = state["packages"].get(name) if isinstance(state["packages"].get(name), dict) else {}
            out.append(self._describe_package(ws, pkg, entry_state))
        return {"initialized": True, "workspace": str(ws), "packages": out}

    def _describe_package(self, ws: Path, pkg: dict[str, Any], entry_state: dict[str, Any]) -> dict[str, Any]:
        pj = pkg["package_json"]
        compat = rw.engine_compatible(pj, self._host_version())
        enabled = bool(entry_state.get("enabled", True))
        status = entry_state.get("status") or ("enabled" if enabled else "disabled")
        contribution = self._contribution_preview(Path(pkg["dir"]), pj)
        return {
            "name": pkg["name"],
            "version": pkg["version"],
            "status": status,
            "enabled": enabled,
            "engines_dsh": rw.dsh_engine_range(pj),
            "engines_compatible": compat,
            "contribution": contribution,
            "description": str(pj.get("description") or ""),
            "requested_version": entry_state.get("requested_version"),
            "error": entry_state.get("error"),
            # 装后 peer 完整性自检结果（生态实证：legacy 装法不补 peer，如实可见）
            "peer_gaps": entry_state.get("peer_gaps"),
            "legacy_peers": entry_state.get("legacy_peers"),
        }

    def _host_version(self) -> str:
        from translator import DSH_SOURCE_VERSION

        return DSH_SOURCE_VERSION

    def _contribution_preview(self, pkg_dir: Path, pj: dict[str, Any]) -> dict[str, Any]:
        """翻译预览摘要（M3）：工具形/主题数/页面数/钩子形——静态 translator dry-run。"""
        out: dict[str, Any] = {"tools": False, "tool_names": [], "themes": 0, "pages": 0, "hooks": 0}
        try:
            manifest = translate_package(pkg_dir)
        except (FileNotFoundError, ValueError, OSError):
            return out
        kinds = manifest.get("kinds", {})
        out["tools"] = "tool" in kinds
        out["hooks"] = len(kinds.get("hook", {}).get("events", []))
        # 工具名最佳努力静态提取（lib 入口 defineTool 形态）；运行时权威 = 桥自省
        for candidate in (pkg_dir / "lib" / "index.js",):
            if candidate.is_file():
                try:
                    blob = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                out["tool_names"] = sorted(set(_TOOL_NAME_RE.findall(blob)))[:20]
        # 主题数（skin-manifest-v2 资产目录）
        skins = self._skin_ids_of(pkg_dir)
        out["themes"] = len(skins)
        return out

    def _skin_ids_of(self, pkg_dir: Path) -> list[str]:
        skins = pkg_dir / "skins"
        if not skins.is_dir():
            return []
        out = []
        for d in sorted(skins.iterdir()):
            if d.is_dir() and (d / "skin.json").is_file():
                out.append(d.name)
        return out

    # ── install（审批 + engines 拦截 + 缺省版本）─────────────

    async def install(self, pkg: str, version: str | None = None,
                      legacy_peers: bool = False) -> dict[str, Any]:
        """安装第三方 npm 包：版本解析 → engines 拦截 → 翻译预览 → 审批 → 落盘。

        Args:
            legacy_peers: 以 ``--legacy-peer-deps`` 装法安装（生态实证：0.1-rc
                时代社区包的 peer 声明与 0.2.1 工作区冲突 → npm ERESOLVE）。
                该装法不自动补 peer，装后自检缺失 peer 并以 ``--force`` 补树
                （fail-visible：仍缺的 peer 落 state ``peer_gaps``）。

        Returns:
            ``{status: "pending_approval", request_id, preview}``——安装由背景
            任务续行，终态经 ``list_installed`` 可见（AC-R3 状态列）。
        """
        pkg = str(pkg).strip()
        version = (str(version).strip() if version else None) or None
        if not _valid_pkg_name(pkg):
            # 形态守门：裸名或 @scope/name；带路径/参数注入形态直接拒
            raise ManagerError("invalid_request", f"invalid package name: {pkg!r}", package=pkg)
        ws = self._workspace()
        spec, index_entry = await self._resolve_version(pkg, version)
        self._check_engines(pkg, spec)
        preview = await self._build_preview(pkg, spec)
        preview["legacy_peers"] = bool(legacy_peers)
        return await self._approve_then_install(ws, pkg, spec, preview,
                                                legacy_peers=bool(legacy_peers))

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

    def _check_engines(self, pkg: str, spec: str) -> None:
        rc, out, err = self._npm_runner_blocking(["npm", "view", spec, "--json"])
        if rc != 0:
            raise self._npm_error(err, pkg)
        try:
            meta = json.loads(out)
        except ValueError as exc:
            raise ManagerError("registry_unreachable", f"npm view 输出非 JSON: {exc}", package=pkg) from exc
        engines = (meta.get("engines") or {}) if isinstance(meta, dict) else {}
        dsh_range = engines.get("dsh") if isinstance(engines, dict) else None
        compat = rw.engine_compatible({"engines": {"dsh": dsh_range} if dsh_range else {}},
                                      self._host_version())
        if compat is False:
            raise ManagerError(
                "engines_incompatible",
                f"包 {pkg} 声明 engines.dsh={dsh_range}，与本适配器基准 "
                f"{self._host_version()} 不兼容——安装前拦截",
                package=pkg, engines=dsh_range, host=self._host_version(),
            )

    def _npm_runner_blocking(self, cmd: list[str]) -> tuple[int, str, str]:
        return self._npm_runner(cmd, self._npm_timeout_s)

    def _npm_error(self, err: str, pkg: str) -> ManagerError:
        text = (err or "").strip()
        if "E404" in text or "404" in text.splitlines()[0:1] or "not found" in text.lower():
            return ManagerError("package_not_found", f"包不存在（registry 404）: {pkg}", package=pkg)
        return ManagerError("registry_unreachable", f"registry 不可达或查询失败: {text[:200]}", package=pkg)

    async def _build_preview(self, pkg: str, spec: str) -> dict[str, Any]:
        """翻译预览（审批卡内容 / M3）：npm pack 下载 → 解包 → translator dry-run。"""
        preview: dict[str, Any] = {"package": pkg, "spec": spec, "method": "translator-dry-run"}
        with tempfile.TemporaryDirectory(prefix="dsh-preview-") as tmp:
            tmp_path = Path(tmp)
            rc, out, err = await self._run_npm(
                ["npm", "pack", spec, "--pack-destination", str(tmp_path),
                 "--registry", rw.NPM_REGISTRY_DEFAULT, "--no-audit", "--no-fund"])
            if rc != 0:
                raise self._npm_error(err, pkg)
            tarballs = list(tmp_path.glob("*.tgz"))
            if not tarballs:
                raise ManagerError("registry_unreachable", f"npm pack 零产物: {err[:160]}", package=pkg)
            pkg_dir = _extract_tarball(tarballs[0], tmp_path / "x")
            preview.update(self._contribution_preview(pkg_dir, _read_json(pkg_dir / "package.json") or {}))
        return preview

    async def _approve_then_install(self, ws: Path, pkg: str, spec: str,
                                    preview: dict[str, Any],
                                    legacy_peers: bool = False) -> dict[str, Any]:
        """审批卡（翻译预览）→ 裁决 → 背景安装（pending 即同构挂起/恢复语义）。"""
        cap = self._capability()
        if cap is None:
            raise ManagerError(
                "approval_unavailable",
                "审批通道不可用（human-interaction capability 未注入）——安装被拒，"
                "第三方代码未经审批不进执行面",
                package=pkg,
            )
        try:
            created = await cap.call("create_choice", {
                "title": f"DSH 插件安装审批：{pkg}",
                "description": "翻译预览（装什么注册什么）：\n" + json.dumps(preview, ensure_ascii=False, indent=1),
                "options": [
                    {"id": "approve", "label": "批准安装"},
                    {"id": "deny", "label": "拒绝"},
                ],
                "timeout_seconds": int(self._approval_timeout_s),
            }, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 — capability 通道故障=审批不可用（fail-visible）
            raise ManagerError("approval_unavailable",
                               f"审批通道调用失败: {exc}", package=pkg) from exc
        request_id = str((created or {}).get("request_id") or "")
        if not request_id:
            raise ManagerError("approval_unavailable",
                               f"审批请求创建失败: {created!r}", package=pkg)
        rw.mark_package_state(ws, pkg, status="pending_approval", requested_version=spec.rsplit("@", 1)[-1],
                              preview=preview, legacy_peers=legacy_peers)
        task = asyncio.create_task(self._finish_install(ws, pkg, spec, request_id,
                                                       legacy_peers=legacy_peers))
        self._npm_tasks[pkg] = task
        return {"status": "pending_approval", "request_id": request_id,
                "package": pkg, "spec": spec, "preview": preview}

    def _capability(self) -> Any | None:
        if self._capability_factory is None:
            return None
        try:
            return self._capability_factory()
        except (KeyError, AttributeError):
            return None

    async def _finish_install(self, ws: Path, pkg: str, spec: str, request_id: str,
                              legacy_peers: bool = False) -> None:
        """背景续行：等待裁决 → 批准则 npm install + 皮肤同步 + 状态终态。"""
        cap = self._capability()
        if cap is None:
            # 审批通道中途消失 = 拒绝不放行（fail-visible 留痕）
            rw.mark_package_state(ws, pkg, status="failed",
                                  error="approval channel unavailable at wait time")
            return
        try:
            waited = await cap.call("wait_for_choice", {"request_id": request_id},
                                    timeout=self._approval_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 等待通道故障=拒绝不放行（fail-visible 留痕）
            rw.mark_package_state(ws, pkg, status="failed",
                                  error=f"approval wait failed: {exc}")
            return
        response = (waited or {}).get("response") or {}
        selected = response.get("selected_option")
        status = (waited or {}).get("status")
        approved = status == "answered" and selected == "approve"
        if not approved:
            try:
                await cap.call("cancel", {"request_id": request_id}, timeout=10.0)
            except Exception:  # noqa: BLE001 — 取消是收尾尽力而为
                pass
            rw.mark_package_state(ws, pkg, status="approval_denied",
                                  error="安装被拒绝或超时（超时=拒绝不放行）")
            return
        rc, _out, err = await self._run_npm(
            rw.npm_install_command(ws, [spec], legacy_peers=legacy_peers))
        if rc != 0:
            rw.mark_package_state(ws, pkg, status="failed", error=f"npm install failed: {err[:300]}")
            return
        gaps = self._peer_gaps(ws)
        if gaps and legacy_peers:
            # --legacy-peer-deps 不自动补 peer（生态实证）：--force 补树一次，
            # 仍缺的 peer 如实落 state（fail-visible，不假装完整）
            logger.warning("[dsh_manager] 装后 peer 缺失 %d 项，--force 补树: %s", len(gaps), gaps)
            rc, _out, err = await self._run_npm(rw.npm_repair_command(ws))
            if rc != 0:
                logger.warning("[dsh_manager] peer 补树失败（保留已装态 + gaps 可见）: %s", err[:200])
            gaps = self._peer_gaps(ws)
        elif gaps:
            logger.warning("[dsh_manager] 装后 peer 缺失 %d 项（未启用 legacy_peers，如实记录）: %s",
                           len(gaps), gaps)
        self._post_install(ws, pkg)
        rw.mark_package_state(ws, pkg, status="installed", enabled=True, error=None,
                              peer_gaps=gaps or None)

    def _peer_gaps(self, ws: Path) -> list[dict[str, str]]:
        """peer 完整性自检：逐包比对 peerDependencies 与已装集（optional 跳过）。"""
        installed = {p["name"]: p for p in rw.installed_packages(ws)}
        gaps: list[dict[str, str]] = []
        for name, entry in installed.items():
            meta = entry["package_json"]
            peers = meta.get("peerDependencies")
            if not isinstance(peers, dict):
                continue
            peer_meta = meta.get("peerDependenciesMeta")
            optional = peer_meta if isinstance(peer_meta, dict) else {}
            for peer, rng in sorted(peers.items()):
                if peer in installed:
                    continue
                if isinstance(optional.get(peer), dict) and optional[peer].get("optional") is True:
                    continue
                gaps.append({"package": name, "peer": str(peer), "range": str(rng)})
        return gaps

    def _post_install(self, ws: Path, pkg: str) -> None:
        """装后翻译面落位：皮肤包资产同步进皮肤目录（主题选择器即现，M2）。"""
        for p in rw.installed_packages(ws):
            if p["name"] != pkg:
                continue
            skins = self._install_skin_dirs(Path(p["dir"]))
            if skins:
                rw.mark_package_state(ws, pkg, skins=skins)
            break

    def _install_skin_dirs(self, pkg_dir: Path) -> list[str]:
        skins = self._skin_ids_of(pkg_dir)
        if not skins:
            return []
        dest_root = Path(self.skins_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        for skin in skins:
            src = pkg_dir / "skins" / skin
            dest = dest_root / skin
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        return skins

    # ── uninstall（依赖保护 + 皮肤回收）──────────────────────

    async def uninstall(self, pkg: str) -> dict[str, Any]:
        ws = self._workspace()
        installed = {p["name"]: p for p in rw.installed_packages(ws)}
        if pkg not in installed:
            raise ManagerError("package_not_installed", f"包未安装: {pkg}", package=pkg)
        dependents = self._dependents_of(installed, pkg)
        if dependents:
            raise ManagerError("has_dependents",
                               f"包被其他已装包依赖，先卸载依赖者: {', '.join(dependents)}",
                               package=pkg, dependents=dependents)
        rc, _out, err = await self._run_npm(rw.npm_uninstall_command(ws, [pkg]))
        if rc != 0:
            raise ManagerError("npm_error", f"npm uninstall 失败: {err[:300]}", package=pkg)
        # 皮肤目录回收 + 状态清理
        entry = rw.package_state(ws, pkg)
        for skin in entry.get("skins", []) or []:
            shutil.rmtree(Path(self.skins_dir) / skin, ignore_errors=True)
        state = rw.read_state(ws)
        state["packages"].pop(pkg, None)
        rw.write_state(ws, state)
        return {"ok": True, "package": pkg}

    def _dependents_of(self, installed: dict[str, dict[str, Any]], pkg: str) -> list[str]:
        out = []
        for name, p in sorted(installed.items()):
            if name == pkg or name in rw.BOOT_PEER_PACKAGES:
                continue
            pj = p["package_json"]
            refs: dict[str, Any] = {}
            refs.update(pj.get("dependencies") or {})
            refs.update(pj.get("peerDependencies") or {})
            for dep_name in refs:
                if dep_name == pkg:
                    out.append(name)
                    break
        return out

    # ── enable / disable（AC-R3：状态即生效）─────────────────

    async def enable(self, pkg: str) -> dict[str, Any]:
        return await self._set_enabled(pkg, True)

    async def disable(self, pkg: str) -> dict[str, Any]:
        return await self._set_enabled(pkg, False)

    async def _set_enabled(self, pkg: str, enabled: bool) -> dict[str, Any]:
        ws = self._workspace()
        installed = {p["name"] for p in rw.installed_packages(ws)}
        if pkg not in installed:
            raise ManagerError("package_not_installed", f"包未安装: {pkg}", package=pkg)
        entry = rw.package_state(ws, pkg)
        skins = entry.get("skins", []) or []
        if skins:
            self._reconcile_skins(pkg, skins, enabled)
        rw.mark_package_state(ws, pkg, enabled=enabled, status="enabled" if enabled else "disabled")
        return {"ok": True, "package": pkg, "enabled": enabled}

    def _reconcile_skins(self, pkg: str, skins: list[str], enabled: bool) -> None:
        """皮肤包启停 = 皮肤目录同步/回收（主题选择器即现/即消，M2 逆向）。"""
        dest_root = Path(self.skins_dir)
        if enabled:
            ws = Path(self.runtime_root)
            for p in rw.installed_packages(ws):
                if p["name"] == pkg:
                    for skin in skins:
                        src = Path(p["dir"]) / "skins" / skin
                        if src.is_dir():
                            dest = dest_root / skin
                            if dest.exists():
                                shutil.rmtree(dest)
                            shutil.copytree(src, dest)
                    break
        else:
            for skin in skins:
                shutil.rmtree(dest_root / skin, ignore_errors=True)

    # ── install_local（测试通道专用；不走审批）───────────────

    async def install_local(self, source_dir: str, *, skip_peers: bool = False) -> dict[str, Any]:
        """本地目录装入工作区（dsh_plugins/ 样本测试通道；非第三方 registry
        拉取，无审批面）。engines 对本地 package.json 直接判。"""
        src = Path(source_dir).resolve()
        pj = _read_json(src / "package.json")
        if not pj:
            raise ManagerError("invalid_request", f"非 npm 包目录（无 package.json）: {src}", source=str(src))
        pkg = str(pj.get("name") or src.name)
        compat = rw.engine_compatible(pj, self._host_version())
        if compat is False:
            raise ManagerError("engines_incompatible",
                               f"包 {pkg} engines.dsh={rw.dsh_engine_range(pj)} 与基准 "
                               f"{self._host_version()} 不兼容",
                               package=pkg, engines=rw.dsh_engine_range(pj))
        ws = self._workspace()
        rc, _out, err = await self._run_npm(rw.npm_install_command(ws, [str(src)]))
        if rc != 0:
            raise ManagerError("npm_error", f"npm install（本地）失败: {err[:300]}", package=pkg)
        skins = self._install_skin_dirs(src)
        rw.mark_package_state(ws, pkg, status="installed", enabled=True, requested_version=pj.get("version"))
        if skins:
            rw.mark_package_state(ws, pkg, skins=skins)
        return {"status": "installed", "package": pkg, "skins": skins}

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


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
