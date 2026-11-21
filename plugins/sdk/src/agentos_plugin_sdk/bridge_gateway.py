"""MCP Bridge 网关生命周期（按需自动拉起 + 健康检查 + token 自给）。

为什么需要本模块：MCP 客户端（``browser_tool`` 等，见 ``bridge_client``）走
HTTP 连宿主 Bridge 网关（默认 ``http://127.0.0.1:8765``，进程实现
``mcp-servers/mcp-bridge/server.py``）。网关是独立常驻进程，此前唯一入口是
人工执行 ``scripts/start_bridge.bat``，且网关 fail-closed 要求预先设置
``AGENTOS_BRIDGE_TOKEN``——用户不手工起进程、不手工造密钥，工具就必然报
"Connection refused"。生命周期无归属即缺陷，本模块把归属补上：

- **按需拉起**：客户端连不上时启动网关（幂等，跨进程文件锁防重复 spawn）；
- **就绪探测**：轮询 ``GET /health`` 直到可服务，超时报错附日志尾部；
- **token 自给**：配置/环境变量都缺时自生成并落用户空间 ``.env``（0600），
  网关与客户端共用同一份凭据，用户无需关心；
- **自愈**：每次连接层失败都会重新走一遍 ensure，网关崩溃后下次调用即恢复。

设计约束（见 ADR 2026-09-10-mcp-bridge-gateway / 2026-11-20-mcp-bridge-autostart）：
网关本身仍是独立进程（沙箱→宿主治理层，认证/白名单/审计），不并入 sidecar，
也不动内核。本模块只补"谁负责让它跑起来"。

环境旋钮（SDK 侧读取，非内核 env 键）：
- ``AGENTOS_PROJECT_ROOT``：仓库/安装根（定位网关脚本与 .env）；
- ``AGENTOS_BRIDGE_AUTOSTART``：置 0/false/off 关闭自动拉起（只报错不起进程）；
- ``AGENTOS_BRIDGE_TOKEN``：预置凭据（优先于自生成）；
- ``AGENTOS_BRIDGE_LOG``：网关 stdout/stderr 落盘路径（默认 logs/mcp-bridge/gateway.log）。
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

TOKEN_KEY = "AGENTOS_BRIDGE_TOKEN"  # noqa: S105 — 环境变量名，非密钥字面量
TOKEN_KEY_BYTES = TOKEN_KEY + "="
AUTOSTART_ENV = "AGENTOS_BRIDGE_AUTOSTART"
PROJECT_ROOT_ENV = "AGENTOS_PROJECT_ROOT"
GATEWAY_LOG_ENV = "AGENTOS_BRIDGE_LOG"

# 网关脚本相对根目录（仓库布局 = 安装布局 resources/ 同构）。
GATEWAY_REL = ("mcp-servers", "mcp-bridge", "server.py")
# 本机地址族：命中即以"网关应在本机运行"为前提执行拉起（远端地址不自作主张）。
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})  # noqa: S104 — 地址比对表，非监听绑定

HEALTH_TIMEOUT_SECS = 2.0
SPAWN_WAIT_SECS = 30.0
POLL_SECS = 0.25


class BridgeGatewayError(Exception):
    """网关不可用且无法自动拉起（含已尝试的修复动作与日志位置）。"""


def is_local_base(base_url: str) -> bool:
    """base 是否指向本机（决定是否允许自动拉起）。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(base_url).hostname or "").strip().lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def autostart_enabled() -> bool:
    """自动拉起开关（缺省开；显式 0/false/off 关闭）。"""
    raw = os.environ.get(AUTOSTART_ENV, "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def project_root() -> str:
    """定位含网关脚本的根。

    两种布局都认：仓库布局 ``<root>/mcp-servers/mcp-bridge/server.py``，以及
    装机布局 ``<root>/resources/mcp-servers/mcp-bridge/server.py``
    （electron-builder extraResources 落位）。只认前者会让装机态 ensure 报
    "找不到网关脚本"——装机是主战场，两种布局必须同等对待。
    """
    for base in _root_candidates():
        if os.path.isfile(os.path.join(base, *GATEWAY_REL)):
            return base
        packaged = os.path.join(base, "resources")
        if os.path.isfile(os.path.join(packaged, *GATEWAY_REL)):
            return packaged
    env_root = os.environ.get(PROJECT_ROOT_ENV, "").strip()
    return env_root if env_root else ""


def _root_candidates() -> list[str]:
    """候选根：显式 env → cwd 向上逐级（含自身）。"""
    out: list[str] = []
    env_root = os.environ.get(PROJECT_ROOT_ENV, "").strip()
    if env_root:
        out.append(env_root)
    cur = os.getcwd()
    for _ in range(8):
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


def gateway_script(root: str) -> str:
    return os.path.join(root, *GATEWAY_REL)


def gateway_log_path(root: str) -> str:
    """网关日志路径（stdout/stderr 合流）。"""
    explicit = os.environ.get(GATEWAY_LOG_ENV, "").strip()
    if explicit:
        return explicit
    return os.path.join(root, "logs", "mcp-bridge", "gateway.log")


def probe(base_url: str, timeout: float = HEALTH_TIMEOUT_SECS) -> dict[str, Any]:
    """``GET /health`` 三态探测：{reachable, auth_ok, sessions}。

    auth_ok=False 表示**网关活着但没凭据**（fail-closed 下所有鉴权调用都会 401）：
    这种"伪就绪"必须能被识别，否则客户端会把 401 当凭据配错、把排查方向带偏。
    旧版网关的 /health 不返回 auth 字段 → 按 True 处理（向后兼容，凭据问题由
    后续 401 暴露）。
    """
    url = base_url.rstrip("/") + "/health"
    req = urllib.request.Request(url, method="GET")  # noqa: S310 — 本机网关存活探针，无外部输入
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — base 来自运维配置/本机推导
            if not (200 <= resp.status < 300):
                return {"reachable": False, "auth_ok": False, "sessions": 0}
            raw = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"reachable": False, "auth_ok": False, "sessions": 0}
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    sessions = body.get("sessions") if isinstance(body, dict) else 0
    auth_ok = bool(body.get("auth", True)) if isinstance(body, dict) else True
    return {"reachable": True, "auth_ok": auth_ok, "sessions": int(sessions or 0)}


def health(base_url: str, timeout: float = HEALTH_TIMEOUT_SECS) -> bool:
    """网关是否**可用**（可达且已配凭据）。存活≠可用，故按 probe 的 auth 判定。"""
    state = probe(base_url, timeout)
    return bool(state["reachable"] and state["auth_ok"])


def _env_file_candidates(root: str) -> list[str]:
    """凭据文件候选：用户空间优先（装机侧唯一可写面），其次仓库根。"""
    return [
        os.path.join(root, "user_root", ".env"),
        os.path.join(root, ".env"),
    ]


def read_token_from_files(root: str) -> str:
    for path in _env_file_candidates(root):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(TOKEN_KEY_BYTES):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if value:
                            return value
        except OSError:
            continue
    return ""


def persist_token(root: str, token: str) -> str:
    """把 token 写入凭据文件（用户空间优先，0600）。返回落盘路径（失败返回空串）。"""
    targets = _env_file_candidates(root)
    existing = [p for p in targets if os.path.isfile(p)]
    # 已有文件优先（不新建多余凭据面）；都没有则落用户空间（装机可写面）。
    for path in existing:
        if _append_token_line(path, token):
            return path
    for path in targets:
        if os.path.isfile(path):
            continue
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if _append_token_line(path, token):
            return path
    return ""


def _append_token_line(path: str, token: str) -> bool:
    """写入 token 行。新文件以 0600 首建（不留明文可读窗口），已有文件追加。

    先建后写而非先写后 chmod：后者在 umask 默认 0644 下会把明文凭据短暂暴露给
    本机其他用户（窗口期竞态）。Windows 无 POSIX 权限位，依赖目录 ACL——见 ADR。
    """
    existed = os.path.exists(path)
    try:
        if existed:
            # 已有凭据行则以既有为准（并发/读取失败重入时不再追加第二条）
            existing = read_token_from_files(os.path.dirname(os.path.dirname(path)) or ".")
            if existing:
                return True
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n{TOKEN_KEY}={token}\n")
            return True
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, f"{TOKEN_KEY}={token}\n".encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def resolve_token(config_token: str = "") -> str:
    """凭据解析：调用方配置 → 环境变量 → 凭据文件 → 自生成落盘。

    自生成是"开箱即用"的必需一步：网关 fail-closed，凭据缺失时用户既要起
    进程又要造密钥；本函数让两者都不需要（.env 与网关子进程同源）。
    """
    explicit = str(config_token or "").strip()
    if explicit:
        return explicit
    env_token = os.environ.get(TOKEN_KEY, "").strip()
    if env_token:
        return env_token
    root = project_root()
    if root:
        file_token = read_token_from_files(root)
        if file_token:
            return file_token
    if not root:
        raise BridgeGatewayError(
            "无法定位项目/安装根（找不到 mcp-servers/mcp-bridge/server.py），无法生成并共享网关凭据。"
            f"设置 {PROJECT_ROOT_ENV} 指向项目根或安装 resources 目录后重试"
        )
    token = secrets.token_urlsafe(32)
    persisted = persist_token(root, token)
    if not persisted:
        raise BridgeGatewayError(
            f"网关凭据无法落盘（{root} 下的 .env 不可写），无法与网关共享同一凭据。"
            "请确认安装目录可写，或用 AGENTOS_BRIDGE_TOKEN 预置凭据"
        )
    os.environ.setdefault(TOKEN_KEY, token)
    return token


def _interpreter_candidates(root: str) -> list[str]:
    cands = [sys.executable]
    for exe in ("python3", "python"):
        found = shutil.which(exe)
        if found:
            cands.append(found)
    if root:
        for sub in (
            os.path.join("plugins", "shared", "browser", ".venv", "bin", "python"),
            os.path.join("plugins", "shared", "browser", ".venv", "Scripts", "python.exe"),
            os.path.join(".venv", "bin", "python"),
            os.path.join(".venv", "Scripts", "python.exe"),
        ):
            cands.append(os.path.join(root, sub))
    seen: set[str] = set()
    ordered: list[str] = []
    for c in cands:
        key = os.path.normcase(c)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(c)
    return ordered


def resolve_interpreter(root: str) -> str:
    """挑一个能 import yaml 的解释器（网关依赖 pyyaml）。

    网关是宿主常驻进程，未必由 SDK 的 venv 解释器承载——逐候选探测
    ``import yaml``，命中即用；全不命中给出候选清单供排障。
    """
    tried: list[str] = []
    for cand in _interpreter_candidates(root):
        if not os.path.isfile(cand):
            continue
        tried.append(cand)
        try:
            proc = subprocess.run(  # noqa: S603 — 候选来自解释器解析，无外部输入
                [cand, "-c", "import yaml"],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return cand
    raise BridgeGatewayError(
        "找不到可运行网关的 Python 解释器（要求可 import yaml）。已尝试: " + (", ".join(tried) or "无")
    )


@contextlib.contextmanager
def _spawn_lock(path: str) -> Iterator[bool]:
    """跨进程互斥（非阻塞）：防止并发调用各起一个网关。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        handle = open(path, "a+b")  # noqa: SIM115 — with 语句管理生命周期
    except OSError:
        yield True
        return
    acquired = False
    try:
        if os.name == "nt":  # pragma: no cover - Windows 分支
            import msvcrt

            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        yield acquired
    finally:
        if acquired:  # pragma: no cover - 释放失败无需处理（进程退出即释放）
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


LOG_MAX_BYTES = 8 * 1024 * 1024


def _rotate_log(log_path: str, max_bytes: int = LOG_MAX_BYTES) -> None:
    """超阈值则轮转一份（rename .1，丢弃更早）——常驻网关日志不无限增长。"""
    try:
        if os.path.getsize(log_path) <= max_bytes:
            return
    except OSError:
        return
    with contextlib.suppress(OSError):
        os.replace(log_path, log_path + ".1")


def _spawn(root: str, token: str, log_path: str) -> subprocess.Popen[bytes]:
    """拉起网关子进程（脱离当前会话：调用方退出/重启不影响网关）。"""
    interp = resolve_interpreter(root)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    _rotate_log(log_path)
    env = dict(os.environ)
    env[TOKEN_KEY] = token
    env.setdefault(PROJECT_ROOT_ENV, root)
    log = open(log_path, "ab")  # noqa: SIM115 — 交给子进程持有
    kwargs: dict[str, Any] = {}
    if os.name == "nt":  # pragma: no cover - Windows 分支
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(  # noqa: S603 — argv 固定（解释器 + 网关脚本），无外部输入
            [interp, gateway_script(root)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    finally:
        log.close()


def _log_tail(log_path: str, limit: int = 800) -> str:
    try:
        with open(log_path, "rb") as f:
            data = f.read()[-limit:]
        return data.decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _wait_health(base_url: str, wait_secs: float) -> bool:
    deadline = time.monotonic() + wait_secs
    while time.monotonic() < deadline:
        if health(base_url):
            return True
        time.sleep(POLL_SECS)
    return health(base_url)


def ensure_gateway(base_url: str, token: str = "", wait_secs: float = SPAWN_WAIT_SECS) -> str:
    """确保网关在跑。返回 ``"up"``（本已就绪）或 ``"started"``（本次拉起）。

    幂等：已健康直接返回；多进程并发时经文件锁串行化，抢不到锁的一方等
    健康探测通过即视为成功（不重复 spawn）。
    """
    state = probe(base_url)
    if state["reachable"] and state["auth_ok"]:
        return "up"
    if state["reachable"] and not state["auth_ok"]:
        # 端口被一个"活着但没凭据"的网关占着（旧版本/手工启动/别处拉起）。
        # 它 fail-closed，任何鉴权调用都 401；spawn 新的会撞端口，只能重启它。
        raise BridgeGatewayError(
            f"Bridge 网关在 {base_url} 运行但未配置凭据（fail-closed，所有鉴权调用都会 401）。"
            f"多为旧版本或手工启动的遗留进程；结束该进程后重试即可——新拉起的网关会从 "
            f"{TOKEN_KEY} 环境变量或凭据文件（user_root/.env）自动取得同源凭据"
        )
    if not is_local_base(base_url):
        raise BridgeGatewayError(
            f"Bridge 网关不可达且地址非本机（{base_url}），不在本机拉起（远端网关需自行保障可用性）"
        )
    if not autostart_enabled():
        raise BridgeGatewayError(
            f"Bridge 网关不可达且自动拉起已关闭（{AUTOSTART_ENV}=0）。手动启动：scripts/start_bridge.bat"
        )
    root = project_root()
    if not root or not os.path.isfile(gateway_script(root)):
        raise BridgeGatewayError(
            "Bridge 网关不可达且找不到网关脚本（期望 <root>/mcp-servers/mcp-bridge/server.py）。"
            f"设置 {PROJECT_ROOT_ENV} 指向项目/安装根后重试"
        )
    log_path = gateway_log_path(root)
    token = token or resolve_token()
    with _spawn_lock(os.path.join(root, "logs", "mcp-bridge", "gateway.spawn.lock")) as acquired:
        if health(base_url):  # 双检：抢锁期间别的进程已拉起
            return "up"
        if not acquired:
            if _wait_health(base_url, wait_secs):
                return "up"
            raise BridgeGatewayError(
                f"Bridge 网关启动中等待超时（{wait_secs:.0f}s），另一进程正在拉取，见日志 {log_path}"
            )
        try:
            proc = _spawn(root, token, log_path)
        except OSError as exc:
            raise BridgeGatewayError(f"网关进程启动失败（{log_path}）: {exc}") from exc
        if _wait_health(base_url, wait_secs):
            return "started"
        code = proc.poll()
        detail = f"（进程退出码 {code}）" if code is not None else ""
        raise BridgeGatewayError(
            f"Bridge 网关拉起后仍不可达{detail}，日志 {log_path}：{_log_tail(log_path) or '空'}"
        )
