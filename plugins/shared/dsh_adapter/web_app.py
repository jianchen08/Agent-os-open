"""官方 DSH Web 应用（含 Plugins 页）监督器。

插件管理界面归 DSH 官方 Web 应用：`@deepseek-ai/dsh-client-ui-plugin-manager`
注册进 DSH Web SPA 的 `plugins.tab` slot，不是独立 HTTP 路由。本模块的职责是
**把官方 Web 应用在本机拉起来并给出访问地址**，供前端 webview 内嵌——不自研
同构列表（自研面已按用户裁定退役）。

三条硬约束（决定实现形态）：

1. **同一 profile**：官方 Web 应用的 `pluginManager` 读 profile 的
   ``dsh.profile.bundles``。只有让它跑在 AgentOS 运行时工作区（
   ``runtime_workspace`` 的 profile 目录）上，两侧才是同一份真值——所见的插件
   列表由构造保证一致，而不是靠对账。
2. **不动 bundles**：官方 Web 全栈（``dsh-base`` + ``dsh-web-app``）经 launcher
   的 ``--patch`` 覆盖层装载，**不写进** ``dsh.profile.bundles``——那样桥的工具
   链会把 Web 组合当成已启用插件去 boot（行集完全不同），破坏既有的 plugin
   工具面。
3. **端口固定**（``WEB_PORT_DEFAULT``，env ``AGENTOS_DSH_WEB_PORT`` 可覆盖）：
   前端以跨源 iframe 内嵌本应用，而宿主 CSP 是静态声明（dev =
   ``frontend/vite.config.ts``，Electron = ``electron/app-protocol.ts`` 的
   ``APP_CSP``），随机端口无法被静态 ``frame-src`` 精确放行——能放行它的唯一
   写法是 ``http://127.0.0.1:*`` 通配，那会把整个回环网段纳入可嵌面。故端口
   收敛为固定值，CSP 只放行这一个精确 origin。取值避开内核 9100/9101 与前端
   dev 6390；真实地址仍从 launcher 打印的 ``dsh web: <url>`` 行解析。

launcher 的 URL 携带进程级一次性 token（换取签名 cookie 后重定向）；本模块原样
回传，由 webview 直接加载该 URL 完成认证。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 官方 Web 组合：launcher（CLI）+ Web 应用 bundle + 它所在的基础 bundle。
CLI_PACKAGE = "@deepseek-ai/dsh"
WEB_APP_BUNDLE = "@deepseek-ai/dsh-web-app"
BASE_BUNDLE = "@deepseek-ai/dsh-base"
STACK_PACKAGES: tuple[str, ...] = (BASE_BUNDLE, WEB_APP_BUNDLE, CLI_PACKAGE)

#: bundle 层装载顺序（官方 ``PROFILE_TEMPLATES.web`` 的顺序：base 在前）。
BUNDLE_ORDER: tuple[str, ...] = (BASE_BUNDLE, WEB_APP_BUNDLE)

NODE_MIN_VERSION = (22, 0, 0)
NODE_BIN_ENV = "AGENTOS_DSH_NODE_BIN"
START_TIMEOUT_ENV = "AGENTOS_DSH_WEB_START_TIMEOUT_S"
START_TIMEOUT_DEFAULT_S = 180.0

#: 官方 Web 应用监听端口（固定值）。
#:
#: 前端把本应用当**跨源 iframe** 内嵌，宿主 CSP 是静态声明（见模块 docstring
#: 第 3 条），随机端口无法被精确放行。故端口收敛为固定值，两处 CSP 只放行该
#: 精确 origin；``WEB_PORT_DEFAULT`` 与两处 CSP 字面量的一致性由
#: ``tests/test_official_ui_csp_origin.py`` 机械对账（漂移即红）。
WEB_PORT_ENV = "AGENTOS_DSH_WEB_PORT"
WEB_PORT_DEFAULT = 9177

WEB_HOME_DIR = ".agentos/web-home"
PROFILE_NAME = "web"
PROFILE_DIR_NAME = "profiles"
BUNDLE_PATCH_FIELD = "dsh.bundle.patch"
NPM_REGISTRY_DEFAULT = "https://registry.npmmirror.com"

_URL_LINE_RE = re.compile(r"dsh web:\s+(?P<url>https?://\S+)")
_DEFAULT_PROFILE_TEMPLATE = ["[]\n"]

#: npm 入口名按平台取。Windows 上 Node 目录里**同时**存在 ``npm.cmd`` 与无扩展名
#: 的 ``npm``（POSIX shell 脚本）；后者交给 ``subprocess`` 会被判为"不是有效的
#: Win32 应用程序"（WinError 193），故 Windows 只认可执行封装名。
_NPM_SHIM_NAMES: tuple[str, ...] = ("npm.cmd", "npm.exe") if os.name == "nt" else ("npm",)


class WebAppError(Exception):
    """官方 Web 应用启动失败（code 可判定，端点按 code 如实展示原因）。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass
class WebAppStatus:
    """官方 Web 应用状态快照（端点原样转 JSON）。"""

    available: bool
    url: str | None = None
    origin: str | None = None
    reason: str | None = None
    starting: bool = False
    pid: int | None = None


@dataclass
class WebAppSupervisor:
    """官方 Web 应用的进程监督（单实例，幂等 start/stop）。"""

    runtime_root: str | Path
    node_bin: str | None = None
    registry: str = NPM_REGISTRY_DEFAULT
    _proc: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _url: str | None = field(default=None, init=False)
    _error: str | None = field(default=None, init=False)
    _starting: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ── 路径与前置条件 ────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return Path(self.runtime_root)

    @property
    def home(self) -> Path:
        """launcher 的 ``$DSH_HOME``（profile 目录经它解析）。"""
        return self.root / WEB_HOME_DIR

    def profile_dir(self) -> Path:
        return self.home / PROFILE_DIR_NAME / PROFILE_NAME

    def status(self) -> WebAppStatus:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._url:
                return WebAppStatus(
                    available=True, url=self._url, origin=_origin_of(self._url),
                    pid=self._proc.pid,
                )
            if self._starting:
                return WebAppStatus(available=False, starting=True,
                                    reason="官方 DSH Web 应用启动中")
            return WebAppStatus(available=False, reason=self._error)

    # ── 前置条件检查 ──────────────────────────────────────────────

    def resolve_node(self) -> str:
        """定位 Node ≥ 22：``AGENTOS_DSH_NODE_BIN`` > PATH 中满足版本的 node。"""
        if self.node_bin:
            version = node_version_of(self.node_bin)
            if version is None:
                raise WebAppError("node_unusable", f"{NODE_BIN_ENV} 指向的 node 不可执行: {self.node_bin}")
            if version < NODE_MIN_VERSION:
                raise WebAppError(
                    "node_too_old",
                    f"Node {_fmt_version(version)} 过低（DSH 官方 Web 应用要求 >= 22）",
                    node=_fmt_version(version), required=">=22", path=self.node_bin,
                )
            return self.node_bin
        found: list[tuple[str, tuple[int, int, int]]] = []
        for candidate in _node_candidates():
            version = node_version_of(candidate)
            if version is None:
                continue
            if version >= NODE_MIN_VERSION:
                return candidate
            found.append((candidate, version))
        detail = ", ".join(f"{path}={_fmt_version(ver)}" for path, ver in found) or "PATH 中无 node"
        raise WebAppError(
            "node_too_old",
            f"未找到 Node >= 22（DSH 官方 Web 应用要求）：{detail}；"
            f"装好 Node 22 后用 {NODE_BIN_ENV} 指向该 node",
            found=[{"path": p, "version": _fmt_version(v)} for p, v in found],
        )

    def stack_ready(self) -> bool:
        return all((self.root / "node_modules" / name / "package.json").is_file()
                   for name in STACK_PACKAGES)

    def install_command(self) -> list[str]:
        """全栈安装命令：profile 由 pnpm 管理时用 pnpm，否则 npm。

        profile 的包管理形态**必须跟随既有形态**：官方 plugin-manager 后续在该
        profile 里跑 pnpm add/remove，若这里用 npm 写树，pnpm 会判定
        ``node_modules was created by npm`` 并拒绝后续变更。故 pnpm 可用即用
        pnpm，仅在没有 pnpm 的 profile 上退 npm。
        """
        version = _host_dsh_version()
        specs = [f"{name}@{version}" for name in STACK_PACKAGES]
        pnpm, pnpm_args = _resolve_pnpm(self.root)
        if pnpm is not None:
            return [pnpm, *pnpm_args, "--dir", str(self.root), "add", *specs,
                    "--save-exact", f"--registry={self.registry}"]
        npm = _resolve_npm(self.resolve_node())
        if npm is None:
            raise WebAppError("npm_missing", "未找到 npm/pnpm（装官方 Web 全栈需要其一）")
        return [npm, "install", "--prefix", str(self.root), "--registry", self.registry,
                "--no-audit", "--no-fund", "--save-exact", "--package-lock=false", *specs]

    def install_stack(self, timeout_s: float = 900.0) -> None:
        """首次安装官方 Web 全栈（精确版本；幂等）。"""
        cmd = self.install_command()
        logger.info("[dsh_web_app] 安装官方 Web 全栈: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WebAppError("install_failed", f"官方 Web 全栈安装失败: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise WebAppError("install_failed", f"官方 Web 全栈安装失败: {tail}", tail=tail)

    def ensure_home(self) -> Path:
        """建 ``$DSH_HOME`` 并把 ``profiles/web`` 指向运行时工作区。

        软链是「同一 profile」的落地手段：launcher 只按 ``$DSH_HOME/profiles/<name>``
        解析 profile 目录，而 AgentOS 的 profile 在运行时工作区根。软链失败
        （Windows 无权限）如实报错，不静默退化成第二个 profile——那会让两侧
        插件列表分叉。
        """
        self._ensure_profile_scaffold()
        profiles = self.home / PROFILE_DIR_NAME
        profiles.mkdir(parents=True, exist_ok=True)
        link = profiles / PROFILE_NAME
        if link.is_symlink() or link.exists():
            if link.resolve() == self.root.resolve():
                return self.home
            raise WebAppError(
                "profile_conflict",
                f"{link} 已存在且不指向运行时工作区（{self.root}）；请清理后重试",
                link=str(link),
            )
        try:
            _link_profile_dir(link, self.root.resolve())
        except (OSError, NotImplementedError) as exc:
            raise WebAppError(
                "profile_link_failed",
                f"无法把 {link} 链接到运行时工作区: {exc}",
            ) from exc
        return self.home

    def _ensure_profile_scaffold(self) -> None:
        """profile 骨架归一（launcher 会覆写 ``cordis.yml``，先补齐缺失件）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        for name, content in (("cordis.yml", _DEFAULT_PROFILE_TEMPLATE[0]),
                              ("cordis.patch.yml", _DEFAULT_PROFILE_TEMPLATE[0])):
            path = self.root / name
            if not path.is_file():
                path.write_text(content, encoding="utf-8")

    def bundle_patch_args(self) -> list[str]:
        """按 bundle 声明拼 ``--patch`` 覆盖层（顺序 = 官方模板的 bundle 顺序）。

        patch 文件清单取自各 bundle 自己的 ``dsh.bundle.patch``（字符串或数组），
        不在此硬编码文件名——官方改版时按声明跟随。
        """
        args: list[str] = []
        for name in BUNDLE_ORDER:
            meta = _read_package_json(self.root / "node_modules" / name)
            if meta is None:
                raise WebAppError("stack_incomplete", f"官方 Web 全栈缺包: {name}")
            patch = (meta.get("dsh") or {}).get("bundle", {}).get("patch")
            files = [patch] if isinstance(patch, str) else list(patch or [])
            if not files:
                raise WebAppError("stack_incomplete", f"{name} 未声明 dsh.bundle.patch")
            for rel in files:
                path = self.root / "node_modules" / name / str(rel)
                if not path.is_file():
                    raise WebAppError("stack_incomplete", f"{name} 的 patch 文件缺失: {path}")
                args.extend(["--patch", str(path)])
        return args

    # ── 生命周期 ──────────────────────────────────────────────────

    def start(self) -> WebAppStatus:
        """启动官方 Web 应用（幂等：已在运行直接返回现状态）。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._url:
                return WebAppStatus(available=True, url=self._url,
                                    origin=_origin_of(self._url), pid=self._proc.pid)
            self._error = None
            self._starting = True
        try:
            url, proc = self._spawn()
        except WebAppError as exc:
            with self._lock:
                self._starting = False
                self._error = exc.message
            raise
        with self._lock:
            self._starting = False
            self._proc = proc
            self._url = url
        logger.info("[dsh_web_app] 官方 Web 应用已就绪: %s", _origin_of(url))
        return WebAppStatus(available=True, url=url, origin=_origin_of(url), pid=proc.pid)

    def _spawn(self) -> tuple[str, subprocess.Popen[str]]:
        node = self.resolve_node()
        if not self.stack_ready():
            self.install_stack()
        home = self.ensure_home()
        launcher = self.root / "node_modules" / CLI_PACKAGE / "lib" / "bin.js"
        if not launcher.is_file():
            raise WebAppError("stack_incomplete", f"launcher 缺失: {launcher}")
        cmd = [node, str(launcher), "--profile", PROFILE_NAME,
               *self.bundle_patch_args(), "--no-open", "--port", str(web_port())]
        env = {**os.environ, "DSH_HOME": str(home), "BROWSER": "none"}
        try:
            proc = subprocess.Popen(  # noqa: S603 - 本地 Node launcher，参数自持
                cmd, cwd=str(self.root), env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WebAppError("spawn_failed", f"官方 Web 应用启动失败: {exc}") from exc
        url = _wait_for_url(proc, timeout_s=_start_timeout_s())
        if url is None:
            code = proc.poll()
            tail = _drain(proc)
            proc.kill() if code is None else None
            raise WebAppError(
                "start_failed",
                "官方 Web 应用未给出访问地址" + (f"（进程已退出 code={code}）" if code is not None else "（超时）")
                + (f": {tail}" if tail else ""),
                tail=tail, exit_code=code,
            )
        return url, proc

    def stop(self) -> None:
        with self._lock:
            proc, self._proc, self._url = self._proc, None, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _start_timeout_s() -> float:
    raw = os.environ.get(START_TIMEOUT_ENV, "").strip()
    try:
        return float(raw) if raw else START_TIMEOUT_DEFAULT_S
    except ValueError:
        return START_TIMEOUT_DEFAULT_S


def web_port() -> int:
    """官方 Web 应用监听端口：默认 ``WEB_PORT_DEFAULT``，``AGENTOS_DSH_WEB_PORT`` 覆盖。

    非法值**不静默回落**：回落会让 CSP 白名单与真实监听端口脱钩，症状是 iframe
    被拦成错误页而日志里无线索。故直接 ``port_invalid`` fail-visible。
    """
    raw = os.environ.get(WEB_PORT_ENV, "").strip()
    if not raw:
        return WEB_PORT_DEFAULT
    try:
        port = int(raw)
    except ValueError:
        raise WebAppError("port_invalid", f"{WEB_PORT_ENV} 不是整数: {raw!r}") from None
    if not 1024 <= port <= 65535:
        raise WebAppError("port_invalid", f"{WEB_PORT_ENV} 超出可用端口范围: {port}")
    return port


def _origin_of(url: str) -> str | None:
    """URL → origin（去 token 与路径）。"""
    match = re.match(r"(?P<origin>https?://[^/]+)", url)
    return match.group("origin") if match else None


def _node_candidates() -> list[str]:
    """PATH 上所有 node 可执行文件（去重，保持顺序）。"""
    out: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        for name in ("node", "node.exe"):
            path = Path(entry) / name
            if path.is_file() and os.access(path, os.X_OK):
                text = str(path)
                if text not in out:
                    out.append(text)
    return out


def node_version_of(binary: str) -> tuple[int, int, int] | None:
    """``<binary> --version`` 解析（不可执行/不可解析返回 None）。"""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_version(proc.stdout)


def _parse_version(text: str) -> tuple[int, int, int] | None:
    parts = (text or "").strip().lstrip("v").split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def _fmt_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _resolve_pnpm(root: Path) -> tuple[str | None, list[str]]:
    """profile 内置 pnpm 解析（复用 runtime_workspace 的单源解析序）。"""
    try:
        import runtime_workspace as rw
    except ImportError:  # pragma: no cover - 裸跑模块时的降级
        return None, []
    return rw.resolve_pnpm_invocation(root)


def _resolve_npm(node_bin: str) -> str | None:
    """优先用 Node 自带的 npm（保证与 launcher 同源运行时）。

    只认 ``CreateProcess`` 能直调的入口：Windows 上 Node 目录同时存在
    ``npm.cmd`` 与**无扩展名的 ``npm``**（POSIX shell 脚本）。裸 ``npm`` 走
    ``subprocess`` 会被判为"不是有效的 Win32 应用程序"（WinError 193），故按
    平台取可执行封装名。``os.access(X_OK)`` 在 Windows 上对任意存在的文件恒为
    真，不能用来筛可执行性，平台名筛选才是判据。
    """
    parent = Path(node_bin).parent
    for name in _NPM_SHIM_NAMES:
        sibling = parent / name
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    return shutil.which("npm")


def _link_profile_dir(link: Path, target: Path) -> None:
    """把 profile 目录指向运行时工作区：优先符号链接，Windows 无特权退目录联接。

    Windows 建目录符号链接需要 ``SeCreateSymbolicLinkPrivilege``（开发者模式或管理员），
    普通权限下抛 ``WinError 1314``；目录联接（junction）不需要该特权，且对 launcher
    等价——``resolve()`` 同样解析到目标、路径穿透读写一致（实测 Python 3.14 +
    Node cwd 解析均正常）。仅在符号链接失败时回退，非 Windows 平台原样抛出。
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            raise
        logger.info("[dsh_web_app] 符号链接不可用（%s），改用目录联接: %s", exc, link)
    proc = subprocess.run(  # noqa: S603 - 固定 cmd 内建，参数自持
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False,
        # cmd.exe 输出随系统区域（中文 Windows=GBK/CP936）；PYTHONUTF8=1 下
        # text=True 的 utf-8 严格解码会把本地化提示读崩（UnicodeDecodeError
        # 比 mklink 失败本身更具破坏性）。输出只进错误消息，容错解码即可。
        errors="replace",
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or proc.stdout or "").strip() or "mklink /J 失败")


def _host_dsh_version() -> str:
    """pin 版本单源（translator.DSH_SOURCE_VERSION）。"""
    from translator import DSH_SOURCE_VERSION

    return DSH_SOURCE_VERSION


def _read_package_json(directory: Path) -> dict[str, Any] | None:
    path = directory / "package.json"
    if not path.is_file():
        return None
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _wait_for_url(proc: subprocess.Popen[str], timeout_s: float) -> str | None:
    """读 launcher 输出直到 ``dsh web: <url>`` 或进程退出/超时。"""
    import time

    deadline = time.monotonic() + timeout_s
    assert proc.stdout is not None  # noqa: S101 - Popen 以 PIPE 创建
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                return None
            time.sleep(0.1)
            continue
        match = _URL_LINE_RE.search(line)
        if match:
            return match.group("url")
        logger.debug("[dsh_web_app] %s", line.rstrip())
    return None


def _drain(proc: subprocess.Popen[str]) -> str:
    """进程已退出时取残留输出（失败原因取证，bounded）。"""
    try:
        out, _ = proc.communicate(timeout=5)
    except (subprocess.SubprocessError, ValueError):
        return ""
    return (out or "").strip()[-400:]


_SUPERVISOR: WebAppSupervisor | None = None


def get_supervisor(runtime_root: str | Path | None = None) -> WebAppSupervisor:
    """监督器单例（运行时工作区根缺省取 ``runtime_workspace`` 解析序）。"""
    global _SUPERVISOR  # noqa: PLW0603

    if runtime_root is not None:
        return WebAppSupervisor(runtime_root=runtime_root)
    if _SUPERVISOR is None:
        import runtime_workspace as rw

        _SUPERVISOR = WebAppSupervisor(runtime_root=rw.resolve_runtime_root())
    return _SUPERVISOR


def reset_supervisor() -> None:
    """复位单例（单测隔离用；不停已起进程）。"""
    global _SUPERVISOR  # noqa: PLW0603

    _SUPERVISOR = None


def ensure_started(runtime_root: str | Path | None = None) -> WebAppStatus:
    """确保官方 Web 应用在跑（幂等；已在跑直接返回现状态）。"""
    return get_supervisor(runtime_root).start()
