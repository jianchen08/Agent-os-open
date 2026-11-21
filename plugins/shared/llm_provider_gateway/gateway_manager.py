"""llm_provider_gateway 网关进程管理（独名模块，测试不 import server）。

设计真值 = docs/working/网关随包分发方案_20261121.md（2026-11-21 修订版）：
网关以 provider 插件自包含拉起，生命周期持有者=本插件（唯一持有者）。

端口三态（启动判定）：
① HTTP 健康探活命中（GET 管理页，任何 HTTP 响应=进程在）→ 复用外部网关，
  不重复拉起（本机开发态/已装个人网关态）；
② 探活失败且端口空闲 → 拉起插件内捆绑 bin/cli-proxy-api（--config 指用户
  空间配置），健康等待 + 意外退出指数退避重启 + 连续失败熔断 + 退出树杀；
③ 探活失败且端口被第三方占用 → fail-visible 明确结果，不拉起不误杀，
  不阻塞内核（llm.yaml 其他车道不受扰）。

配置面单源 = manifest config_files[id=gateway] 注入命名空间（环境变量可
覆盖）；server config（cli-proxy-api 进程 --config 文件）真值 = 用户空间
kernel 播种件（config_files[id=gateway_server]），本模块只做一次性令牌修补
（AUTH_DIR 占位符 → 实际路径；空 management.secret-key → 生成随机密钥），
修补后永不再改写——用户在管理页的改动不被覆盖（升级不丢用户配置）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("llm_provider_gateway")

CONFIG_NAMESPACE = "gateway"
DEFAULT_BASE_URL = "http://127.0.0.1:8317"
MANAGEMENT_PAGE = "/management.html"
AUTH_DIR_TOKEN = "__AGENTOS_AUTH_DIR__"
ENV_BASE_URL = "LLM_GATEWAY_BASE_URL"
ENV_CLIENT_API_KEY = "LLM_GATEWAY_CLIENT_API_KEY"
PLUGIN_DIR = Path(__file__).resolve().parent

# 平台 → 捆绑二进制名（ensure_gateway_binary.py 落 bin/ 的规范名）
BINARY_NAMES: dict[str, str] = {
    "windows": "cli-proxy-api.exe",
    "linux": "cli-proxy-api",
    "darwin": "cli-proxy-api",
}

# Windows 专用 creationflags：POSIX 的 subprocess 不定义该常量，显式兜值
# 使平台分支可在任意系统上被测试覆盖。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_OS_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("windows", ("win", "nt", "cygwin", "msys")),
    ("darwin", ("darwin", "mac")),
    ("linux", ("linux",)),
)

# 空管理密钥（种子原文）——修补为随机生成值；有值（用户设过）不动
_EMPTY_SECRET_RE = re.compile(r'(?m)^(\s*secret-key:\s*)""\s*$')

# fake clock 接缝：时序打桩 setattr(gateway_manager, "_time", fake) 于模块
# 命名空间，不改全局 time.monotonic（项目 fake clock 约定）。
import time as _time  # noqa: E402

SERVER_CONFIG_REL = "plugins/llm_provider_gateway/gateway_server.yaml"


@dataclass(frozen=True)
class GatewayConfig:
    """网关托管参数（gateway 命名空间归一化结果 + 路径面）。

    port 由 base_url 解析（端口三态判定输入）；binary_path 指插件捆绑二进制；
    server_config_path 指用户空间 kernel 播种件（进程 --config 真值）；
    factory_server_config_path 为出厂种子（用户侧缺失时的插件侧兜底播种源，
    AGENTOS_CONFIG_ROOT 缺省时 None）。
    """

    base_url: str
    port: int
    binary_path: Path
    server_config_path: Path
    auth_dir: Path
    log_path: Path
    client_api_key: str = ""
    autostart: bool = True
    health_timeout_secs: float = 2.0
    startup_timeout_secs: float = 15.0
    poll_interval_secs: float = 0.5
    call_timeout_secs: float = 1800.0
    restart_backoff_base_secs: float = 2.0
    restart_backoff_max_secs: float = 60.0
    max_consecutive_failures: int = 5
    stable_uptime_secs: float = 60.0
    factory_server_config_path: Path | None = None


def platform_key(system: str | None = None) -> str:
    """归一化系统名到配置键后缀：windows / darwin / linux / 原样小写。"""
    name = (platform.system() if system is None else system).strip().lower()
    for key, prefixes in _OS_PREFIXES:
        if name.startswith(prefixes):
            return key
    return name or "unknown"


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def read_gateway_config(
    injected: dict[str, Any] | None,
    *,
    system: str | None = None,
    plugin_dir: Path = PLUGIN_DIR,
    user_root_dir: Path | str | None,
    config_root_dir: Path | str | None = None,
) -> GatewayConfig:
    """归一化 gateway 命名空间配置（env 覆盖 > 命名空间 > 默认）。

    Args:
        injected: 注入的配置视图（含 gateway 命名空间）。
        system: 覆盖平台探测（测试用；默认 platform.system()）。
        plugin_dir: 插件目录（bin/ 捆绑二进制定位）。
        user_root_dir: 用户空间根（必填——server config/auths/log 全部落此）。
        config_root_dir: 配置根（出厂种子定位；默认读 AGENTOS_CONFIG_ROOT）。

    Raises:
        ValueError: base_url 非 http(s) 形态或 user_root_dir 缺失（调用方
            翻译为失败详情，不静默回退）。
    """
    if user_root_dir is None:
        raise ValueError("user_root_dir 必填（server.py 传 _root_api.user_root()），用户空间路径面缺不得猜")
    namespace = injected.get(CONFIG_NAMESPACE) if isinstance(injected, dict) else None
    namespace = namespace if isinstance(namespace, dict) else {}
    base_url = str(os.environ.get(ENV_BASE_URL) or namespace.get("base_url") or DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"gateway.base_url 须为 http(s) URL，实得 {base_url!r}")
    parsed = urllib.parse.urlparse(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    os_key = platform_key(system)
    binary_name = BINARY_NAMES.get(os_key, BINARY_NAMES["linux"])
    user_root = Path(user_root_dir)
    config_root = Path(config_root_dir) if config_root_dir else None
    if config_root is None:
        env_config_root = os.environ.get("AGENTOS_CONFIG_ROOT")
        config_root = Path(env_config_root) if env_config_root else None
    client_api_key = str(os.environ.get(ENV_CLIENT_API_KEY) or namespace.get("client_api_key") or "")

    return GatewayConfig(
        base_url=base_url,
        port=port,
        binary_path=plugin_dir / "bin" / binary_name,
        server_config_path=user_root / "config" / SERVER_CONFIG_REL,
        auth_dir=user_root / "config" / "gateway" / "auths",
        log_path=user_root / "logs" / "llm_gateway.log",
        client_api_key=client_api_key,
        autostart=_parse_bool(namespace.get("autostart"), True),
        health_timeout_secs=_positive_float(namespace.get("health_timeout_secs"), 2.0),
        startup_timeout_secs=_positive_float(namespace.get("startup_timeout_secs"), 15.0),
        poll_interval_secs=_positive_float(namespace.get("poll_interval_secs"), 0.5),
        call_timeout_secs=_positive_float(namespace.get("call_timeout_secs"), 1800.0),
        restart_backoff_base_secs=_positive_float(namespace.get("restart_backoff_base_secs"), 2.0),
        restart_backoff_max_secs=_positive_float(namespace.get("restart_backoff_max_secs"), 60.0),
        max_consecutive_failures=_positive_int(namespace.get("max_consecutive_failures"), 5),
        stable_uptime_secs=_positive_float(namespace.get("stable_uptime_secs"), 60.0),
        factory_server_config_path=(
            config_root / "plugins" / "llm_provider_gateway" / "gateway_server.yaml"
            if config_root
            else None
        ),
    )


def management_url(base_url: str) -> str:
    """网关管理页 URL（base_url 尾斜杠归一，拼接结果与入参形态无关）。"""
    return base_url.rstrip("/") + MANAGEMENT_PAGE


def _probe_sync(url: str, timeout_secs: float) -> tuple[bool, str]:
    """同步探活一次：任何 HTTP 响应（含 401/404）= 网关进程在；连接层失败 = 不在。

    代理零注入（ProxyHandler({})）：探活对象恒本机回环，env/Windows 注册表
    系统代理（urllib.getproxies 双源）不得劫持——注册表代理在场时 urlopen
    默认会经系统代理转回环调用（装机面实证恒误判）。"""
    request = urllib.request.Request(url, headers={"User-Agent": "agentos-llm-provider-gateway"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_secs) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # 管理页可能要求凭据（4xx/重定向）：有响应即存活，判活只看连接层
        return True, f"HTTP {exc.code}"
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _maybe_await(value: Any) -> Any:
    """探活/端口探测桩两形态容忍（同步函数或协程函数均可注入）。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def probe_gateway(base_url: str, timeout_secs: float = 2.0) -> tuple[bool, str]:
    """异步探活管理页（同步 socket 阻塞挪线程池，不堵事件循环）。"""
    return await asyncio.to_thread(_probe_sync, management_url(base_url), timeout_secs)


def _port_probe_sync(host: str, port: int, timeout_secs: float) -> tuple[bool, str]:
    """同步端口探测：连接被拒 = 空闲；连上 = 被占（第三方占口判定）。"""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_secs)
    try:
        sock.connect((host, port))
        return False, "connected"
    except OSError as exc:
        return True, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


async def probe_port_free(host: str, port: int, timeout_secs: float = 1.0) -> tuple[bool, str]:
    """异步端口空闲探测（同步 socket 挪线程池）。（free, detail）"""
    return await asyncio.to_thread(_port_probe_sync, host, port, timeout_secs)


def kill_process_tree(pid: int) -> None:
    """收树：Windows taskkill /F /T；POSIX 进程组 SIGKILL
    （spawn 侧 start_new_session 保证组 id = pid）。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _spawn_process(config: GatewayConfig) -> subprocess.Popen[bytes]:
    """真实 spawn：bin/cli-proxy-api --config <用户空间配置>，stdout/stderr 落
    用户空间日志文件（可诊断）；Windows 隐藏窗口，POSIX 独立进程组（树杀面）。"""
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(config.log_path, "ab")  # noqa: SIM115 —— 句柄随 Popen 生命周期，由进程退出自然释放
    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        [str(config.binary_path), "--config", str(config.server_config_path)],
        cwd=str(config.binary_path.parent),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        creationflags=creationflags,
        start_new_session=(os.name != "nt"),
    )


class GatewayProcessManager:
    """网关进程生命周期持有者（本插件唯一持有者）。

    全部外部边界可注入（probe/port_probe/spawner/killer/sleep）——测试零
    真网络零真进程；时序经模块命名空间 ``_time`` fake clock 打桩。
    """

    def __init__(
        self,
        config: GatewayConfig,
        *,
        probe: Any = probe_gateway,
        port_probe: Any = probe_port_free,
        spawner: Any = _spawn_process,
        killer: Any = kill_process_tree,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._config = config
        self._probe = probe
        self._port_probe = port_probe
        self._spawner = spawner
        self._killer = killer
        self._sleep = sleep
        self._proc: Any | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopping = False
        self._breaker_open = False
        self._consecutive_failures = 0
        self._spawned_at = 0.0

    @property
    def config(self) -> GatewayConfig:
        return self._config

    # ── 三态判定 ─────────────────────────────────────────────────────

    async def ensure_running(self) -> dict[str, Any]:
        """端口三态判定（幂等入口）：返回 {action, running, detail, owned}。

        action ∈ reuse（外部网关复用）/ owned_alive（本插件进程仍在）/
        spawned（已拉起且健康）/ disabled（autostart=false）/
        binary_missing（bin 未填充）/ port_conflict（第三方占口）/
        spawn_failed（spawn OSError）/ unhealthy（拉起后健康等待超时）。
        """
        async with self._lock:
            return await self._ensure_locked()

    async def _ensure_locked(self) -> dict[str, Any]:
        self._stopping = False
        if self._proc is not None and self._proc.poll() is None:
            return {
                "action": "owned_alive",
                "running": True,
                "detail": f"本插件持有网关进程存活（pid={self._proc.pid}）",
                "owned": True,
            }
        self._proc = None
        ok, detail = await _maybe_await(self._probe(self._config.base_url, self._config.health_timeout_secs))
        if ok:
            return {"action": "reuse", "running": True, "detail": detail, "owned": False}
        if not self._config.autostart:
            return {
                "action": "disabled",
                "running": False,
                "detail": "gateway.autostart=false——探活未命中且不自动拉起",
                "owned": False,
            }
        # 三态判定先行（端口占用面优先于 bin 存在性——占口结论更可诊断）
        free, port_detail = await _maybe_await(
            self._port_probe("127.0.0.1", self._config.port, 1.0)
        )
        if not free:
            return {
                "action": "port_conflict",
                "running": False,
                "detail": (
                    f"端口 {self._config.port} 被第三方占用且探活未命中网关（{port_detail}）"
                    "——不拉起不误杀；cpa_gateway 车道暂不可用，其他车道不受扰"
                ),
                "owned": False,
            }
        if not self._config.binary_path.is_file():
            return {
                "action": "binary_missing",
                "running": False,
                "detail": (
                    f"捆绑网关二进制缺失：{self._config.binary_path}"
                    "（运行 scripts/ensure_gateway_binary.py 填充 bin/ 后重试）"
                ),
                "owned": False,
            }
        try:
            proc = self._spawner(self._config)
        except OSError as exc:
            return {
                "action": "spawn_failed",
                "running": False,
                "detail": f"拉起 {self._config.binary_path.name} 失败: {exc}",
                "owned": False,
            }
        self._proc = proc
        self._spawned_at = _time.monotonic()
        logger.info(
            "[llm_provider_gateway] spawned %s --config %s (pid=%s)",
            self._config.binary_path.name,
            self._config.server_config_path,
            getattr(proc, "pid", "?"),
        )
        healthy, health_detail = await self._wait_healthy()
        if not healthy:
            self._consecutive_failures += 1
            self._kill_owned()
            return {
                "action": "unhealthy",
                "running": False,
                "detail": f"拉起后健康等待未通过：{health_detail}（进程已树杀，详见 {self._config.log_path}）",
                "owned": False,
            }
        self._arm_exit_watcher()
        return {"action": "spawned", "running": True, "detail": health_detail, "owned": True}

    async def _wait_healthy(self) -> tuple[bool, str]:
        """健康等待：轮询探活直到命中 / 进程提前退出 / 超时界（fake clock 可打桩）。"""
        deadline = _time.monotonic() + self._config.startup_timeout_secs
        while True:
            ok, detail = await _maybe_await(self._probe(self._config.base_url, self._config.health_timeout_secs))
            if ok:
                return True, detail
            if self._proc is not None and self._proc.poll() is not None:
                return False, f"进程提前退出 rc={self._proc.poll()}"
            if _time.monotonic() >= deadline:
                return False, f"健康等待超时（{self._config.startup_timeout_secs:.0f}s）"
            await self._sleep(self._config.poll_interval_secs)

    # ── 意外退出退避重启 ─────────────────────────────────────────────

    def _arm_exit_watcher(self) -> None:
        if self._watcher is not None and not self._watcher.done():
            return  # 同一 watcher 循环内自愈（循环重读 self._proc），不叠加任务
        self._watcher = asyncio.create_task(self._watch_exit())

    async def _watch_exit(self) -> None:
        """意外退出监视：退避重启；连续短命超阈值熔断；存活达稳定界复位计数。"""
        while not self._stopping:
            proc = self._proc
            if proc is None:
                return
            while proc.poll() is None:
                await self._sleep(self._config.poll_interval_secs)
                if self._stopping or self._proc is not proc:
                    return
            if self._stopping or self._proc is not proc:
                return
            uptime = _time.monotonic() - self._spawned_at
            if uptime >= self._config.stable_uptime_secs:
                self._consecutive_failures = 0
            self._consecutive_failures += 1
            if self._consecutive_failures > self._config.max_consecutive_failures:
                self._breaker_open = True
                logger.error(
                    "[llm_provider_gateway] 连续短命 %d 次超过阈值 %d——熔断退避重启（fail-visible），"
                    "详情见 %s",
                    self._consecutive_failures,
                    self._config.max_consecutive_failures,
                    self._config.log_path,
                )
                return
            delay = min(
                self._config.restart_backoff_base_secs * (2 ** (self._consecutive_failures - 1)),
                self._config.restart_backoff_max_secs,
            )
            logger.warning(
                "[llm_provider_gateway] 网关进程意外退出（uptime=%.1fs，连续第 %d 次）——%.1fs 后重拉",
                uptime,
                self._consecutive_failures,
                delay,
            )
            await self._sleep(delay)
            if self._stopping:
                return
            result = await self._ensure_locked()
            if not result.get("running"):
                return
            # 循环继续：watch 新拉起的进程

    def _kill_owned(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            self._killer(proc.pid)
        except OSError as exc:
            logger.warning("[llm_provider_gateway] 树杀 pid=%s 失败: %s", proc.pid, exc)
        self._proc = None

    # ── 状态与停止 ───────────────────────────────────────────────────

    async def detach(self) -> Any:
        """换管移交：停掉退出监视但不杀进程（配置热更换管时由新管 adopt 接手）。"""
        self._stopping = True
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 收尾取消面，不传播
                pass
            self._watcher = None
        proc = self._proc
        self._proc = None
        return proc

    def adopt(self, proc: Any) -> None:
        """接管既有存活进程（不树杀、不重复拉起），恢复退出监视。"""
        if proc is None or proc.poll() is not None:
            return
        self._proc = proc
        self._arm_exit_watcher()

    async def status(self) -> dict[str, Any]:
        """状态视图：探活（外部/本插件进程二态）+ 持有信息。"""
        if self._proc is not None and self._proc.poll() is None:
            return {
                "running": True,
                "owned": True,
                "pid": self._proc.pid,
                "base_url": self._config.base_url,
                "detail": "本插件持有网关进程存活",
            }
        ok, detail = await _maybe_await(self._probe(self._config.base_url, self._config.health_timeout_secs))
        return {
            "running": ok,
            "owned": False,
            "pid": None,
            "base_url": self._config.base_url,
            "detail": detail if ok else f"网关不在（{detail}）",
        }

    async def stop(self) -> dict[str, Any]:
        """停止本插件持有的网关进程（树杀）；外部网关不受本插件管理。"""
        self._stopping = True
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 收尾取消面，不传播
                pass
            self._watcher = None
        proc = self._proc
        if proc is None:
            return {"action": "not_owned", "detail": "无本插件持有的网关进程（外部网关不受扰）"}
        pid = proc.pid
        try:
            self._killer(pid)
        except OSError as exc:
            return {"action": "stop_failed", "detail": f"树杀 pid={pid} 失败: {exc}"}
        self._proc = None
        logger.info("[llm_provider_gateway] 网关进程已收树 (pid=%s)", pid)
        return {"action": "stopped", "detail": f"已收树 pid={pid}", "pid": pid}


def seed_server_config(config: GatewayConfig) -> dict[str, Any]:
    """server config 落位与令牌修补（幂等；升级不丢用户配置）。

    语义：用户空间 kernel 播种件（config_files[id=gateway_server] 的用户层
    副本）为进程 --config 真值。用户侧缺失而出厂侧在 → 逐字播种（内核 A3
    播种的插件侧兜底）。文件含 AUTH_DIR_TOKEN → 原位替换为实际 auth-dir 绝对
    路径；management.secret-key 为空 → 生成随机管理密钥（管理 API 缺密钥时
    上游直接禁用管理面，管理口必须可用）。修补后不再改写——用户在管理页的
    改动（OAuth 凭据、车道配置、密钥）永不被覆盖。

    Returns:
        {path, seeded, token_replaced, secret_generated, detail}（失败面显式
        降级为字段，不抛）。
    """
    path = config.server_config_path
    result: dict[str, Any] = {
        "path": str(path),
        "seeded": False,
        "token_replaced": False,
        "secret_generated": False,
        "detail": "",
    }
    if not path.is_file():
        factory = config.factory_server_config_path
        if factory is None or not factory.is_file():
            result["detail"] = "用户侧与出厂侧 server config 种子均缺失——网关进程无配置可用（fail-visible）"
            return result
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(factory, path)
        result["seeded"] = True
    text = path.read_text(encoding="utf-8")
    changed = False
    if AUTH_DIR_TOKEN in text:
        text = text.replace(AUTH_DIR_TOKEN, str(config.auth_dir))
        result["token_replaced"] = True
        changed = True
    if _EMPTY_SECRET_RE.search(text):
        text = _EMPTY_SECRET_RE.sub(lambda m: m.group(1) + f'"{secrets.token_hex(16)}"', text)
        result["secret_generated"] = True
        changed = True
    if changed:
        _atomic_write(path, text)
    # 运行面目录就位（auth 产物、进程日志）
    config.auth_dir.mkdir(parents=True, exist_ok=True)
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    result["detail"] = "server config 就绪（真值=用户空间，插件不再改写）"
    return result


def _atomic_write(path: Path, text: str) -> None:
    """原子写（临时文件 + replace），避免半写文件被进程读到。"""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "AUTH_DIR_TOKEN",
    "BINARY_NAMES",
    "CONFIG_NAMESPACE",
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "ENV_CLIENT_API_KEY",
    "GatewayConfig",
    "GatewayProcessManager",
    "MANAGEMENT_PAGE",
    "PLUGIN_DIR",
    "kill_process_tree",
    "management_url",
    "platform_key",
    "probe_gateway",
    "probe_port_free",
    "read_gateway_config",
    "seed_server_config",
]
