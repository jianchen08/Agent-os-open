"""MCP Bridge 网关——上游 stdio 子进程管理。

每个 (上游名, 会话 key) 持有一个上游 MCP server 子进程（newline-delimited
JSON-RPC over stdio，与内核 agentos-mcp crate 同款帧协议），按 id 匹配请求
响应；会话空闲超配置阈值后整树回收。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
import time

# 上游响应等待上限（秒）。超出视为上游无响应，调用方丢弃会话重建。
RESPONSE_TIMEOUT_SECS = 120.0

# 请求 id：进程内全局自增（每个上游进程独立 stdin，id 不会跨进程串扰）。
_ID_LOCK = threading.Lock()
_NEXT_ID = [0]

# Windows PATHEXT 缺省序列（npm 生态 npx/npm 是 .cmd 批处理，CreateProcess
# 无扩展名命令只找 .exe——与内核 mcp crate resolve_windows_command 同语义）。
_PATHEXT_DEFAULT = ".COM;.EXE;.BAT;.CMD"


def resolve_windows_command(command: str) -> str:
    """Windows 下解析无扩展名命令为可执行全路径（PATHEXT 语义）。

    逐个 PATH 目录 × PATHEXT 扩展名探测，命中即返回；无命中原样返回
    （保持 spawn 报原始错误）。仅 Windows 生效，其他平台原样返回。
    """
    if os.name != "nt":
        return command
    if Path(command).suffix:
        return command
    pathext = os.environ.get("PATHEXT", _PATHEXT_DEFAULT)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for d in path_dirs:
        if not d:
            continue
        for ext in pathext.split(";"):
            if not ext:
                continue
            cand = Path(d) / f"{command}{ext.lower()}"
            if cand.is_file():
                return str(cand)
    return command


def _next_request_id() -> int:
    with _ID_LOCK:
        _NEXT_ID[0] += 1
        return _NEXT_ID[0]


class UpstreamError(Exception):
    """上游不可用（spawn 失败 / 响应超时 / 进程已死）。"""


class UpstreamSession:
    """单个上游 MCP server 子进程会话。

    线程模型：调用方（HTTP worker 线程）持锁串行化 write-request/read-response，
    同会话内请求串行；不同会话互不阻塞。
    """

    def __init__(self, upstream_name: str, session_key: str, command: list[str], cwd: str | None = None):
        self.upstream_name = upstream_name
        self.session_key = session_key
        self.command = list(command)
        self.last_used = time.monotonic()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._cwd = cwd

    # ── 生命周期 ──────────────────────────────────────────────

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            # 新进程组：回收时整树杀（Windows 用 taskkill /T 兜底）。
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                creationflags = 0
            self._proc = subprocess.Popen(
                [resolve_windows_command(self.command[0])] + self.command[1:],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self._cwd,
                creationflags=creationflags,
                **({} if os.name == "nt" else {"preexec_fn": os.setsid}),
            )
        except OSError as e:
            self._proc = None
            raise UpstreamError(f"上游进程启动失败: {self.command[0] if self.command else ''}: {e}") from e
        return self._proc

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=5)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    # ── 协议 ──────────────────────────────────────────────────

    def request(self, method: str, params: dict | None = None) -> dict:
        """发送 JSON-RPC 请求并等待同 id 响应。无关消息（通知/日志）跳过。"""
        with self._lock:
            proc = self._ensure_process()
            assert proc.stdin is not None and proc.stdout is not None
            req_id = _next_request_id()
            payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            frame = json.dumps(payload, ensure_ascii=False)
            self.last_used = time.monotonic()
            try:
                proc.stdin.write((frame + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise UpstreamError(f"上游写入失败: {e}") from e

            deadline = time.monotonic() + RESPONSE_TIMEOUT_SECS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamError(f"上游响应超时（{RESPONSE_TIMEOUT_SECS:.0f}s）: {method}")
                line = proc.stdout.readline()
                if not line:
                    raise UpstreamError(f"上游 stdout 已关闭: {method}")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != req_id:
                    continue
                if "error" in msg:
                    raise UpstreamError(f"上游返回错误: {msg['error']}")
                return msg.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        """发送 notification（不等待响应）。失败静默——通知不承载请求语义。"""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            assert self._proc.stdin is not None
            payload: dict = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            try:
                self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError):
                pass


class UpstreamManager:
    """按 (上游名, 会话 key) 管理上游会话，空闲回收。"""

    def __init__(self, idle_timeout_secs: float = 300.0):
        self._idle_timeout_secs = idle_timeout_secs
        self._sessions: dict[tuple[str, str], UpstreamSession] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        upstream_name: str,
        session_key: str,
        command: list[str],
        cwd: str | None = None,
    ) -> UpstreamSession:
        key = (upstream_name, session_key)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = UpstreamSession(upstream_name, session_key, command, cwd)
                self._sessions[key] = session
            return session

    def discard(self, upstream_name: str, session_key: str) -> None:
        """丢弃会话进程（上游失败后由调用方触发，下次调用重建）。"""
        with self._lock:
            session = self._sessions.pop((upstream_name, session_key), None)
        if session is not None:
            session.kill()

    def sweep_idle(self) -> list[tuple[str, str]]:
        """回收空闲超限或已死会话，返回被回收的 key 元组列表。"""
        reclaimed: list[tuple[str, str]] = []
        with self._lock:
            now = time.monotonic()
            for key, session in list(self._sessions.items()):
                idle = now - session.last_used
                if idle >= self._idle_timeout_secs or not session.is_alive():
                    session.kill()
                    del self._sessions[key]
                    reclaimed.append(key)
            return reclaimed

    def shutdown_all(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.kill()
            self._sessions.clear()

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)