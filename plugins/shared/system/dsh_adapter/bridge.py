"""DSH Node runtime 桥接宿主（task_dsh_plugin_adapter 任务 4 通道 A）。

管理 ``runtime/dsh-rpc-bridge.mjs`` 子进程的生命周期，经 stdio 上的
newline-delimited JSON-RPC 调用 DSH 工具：

- 惰性启动：首个工具调用时 spawn Node 子进程并 ``initialize``（boot 完整
  DSH cordis context，冷启动以秒计）；
- 失败重启：子进程死亡后下次调用自动重启（``_proc`` 置空，调用方无感）；
- 超时兜底：每调用独立超时（默认 120s，与 bash 工具族上限一致量级）；
- 优雅退出：``shutdown()`` 发 ``shutdown`` 帧并等进程退出，供 on_unload。

协议契约与 mjs 侧对齐（见 runtime/dsh-rpc-bridge.mjs 头注释）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RUNTIME_SCRIPT = Path(__file__).parent / "runtime" / "dsh-rpc-bridge.mjs"
_DEFAULT_REPO_ROOT = r"D:\reference_repos\deepseek-harness-rc8"
_DEFAULT_CWD = os.getcwd()

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INTERNAL = -32000


class BridgeUnavailableError(RuntimeError):
    """DSH runtime 不可用（未配置仓库 / Node 缺失 / 启动失败）。"""


class DshRuntimeBridge:
    """DSH 工具 Node runtime 的单进程宿主（每个 sidecar 插件实例一个）。"""

    def __init__(
        self,
        repo_root: str | None = None,
        cwd: str | None = None,
        call_timeout_s: float = 120.0,
        boot_timeout_s: float = 60.0,
        extra_plugins_dir: str | None = None,
    ) -> None:
        self._repo_root = repo_root or os.environ.get("AGENTOS_DSH_REPO_ROOT") or _DEFAULT_REPO_ROOT
        self._cwd = cwd or _DEFAULT_CWD
        self._call_timeout_s = call_timeout_s
        self._boot_timeout_s = boot_timeout_s
        self._extra_plugins_dir = extra_plugins_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._id_counter = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._server_tools: list[dict[str, Any]] | None = None

    # ── 进程管理 ─────────────────────────────────────────────

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        """确保子进程存活（惰性启动 + 死亡重启）。"""
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        # 清理旧壳（returncode 非None 或 reader 残留）
        await self._teardown(quiet=True)
        if not _RUNTIME_SCRIPT.is_file():
            raise BridgeUnavailableError(f"dsh runtime script missing: {_RUNTIME_SCRIPT}")
        if not Path(self._repo_root).is_dir():
            raise BridgeUnavailableError(
                f"DSH repo root not found: {self._repo_root} "
                "(set AGENTOS_DSH_REPO_ROOT to a built deepseek-harness checkout)"
            )
        env = {**os.environ, "AGENTOS_DSH_REPO_ROOT": self._repo_root}
        if self._extra_plugins_dir:
            env["AGENTOS_DSH_EXTRA_PLUGINS_DIR"] = self._extra_plugins_dir
        self._proc = await asyncio.create_subprocess_exec(
            "node",
            str(_RUNTIME_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # 后台排空 stderr（Node 侧日志），避免管道写满阻塞子进程。
        asyncio.create_task(self._drain_stderr())
        return self._proc

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[dsh-bridge] %s", text)
        except Exception:  # noqa: BLE001 - 排空任务的任何失败都只影响日志
            pass

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    import json

                    frame = json.loads(text)
                except ValueError:
                    logger.warning("[dsh-bridge] bad frame: %s", text[:120])
                    continue
                fut = self._pending.pop(frame.get("id"), None)  # type: ignore[arg-type]
                if fut is not None and not fut.done():
                    fut.set_result(frame)
        finally:
            # 进程退出：唤醒所有等待者（以进程级错误失败）。
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BridgeUnavailableError("dsh runtime process exited"))
            self._pending.clear()

    async def _teardown(self, quiet: bool = False) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if self._proc is not None:
            if self._proc.returncode is None:
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            self._proc = None

    # ── 协议调用 ─────────────────────────────────────────────

    async def _request(self, method: str, params: dict[str, Any], timeout_s: float) -> Any:
        import json

        async with self._lock:
            proc = await self._ensure_proc()
        self._id_counter += 1
        req_id = self._id_counter
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        assert proc.stdin is not None
        proc.stdin.write(f"{json.dumps({'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params})}\n".encode())
        await proc.stdin.drain()
        try:
            frame = await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise
        if "error" in frame:
            err = frame["error"]
            raise RuntimeError(f"dsh bridge error ({err.get('code')}): {err.get('message')}")
        return frame.get("result")

    async def initialize(self) -> list[dict[str, Any]]:
        """boot DSH context 并取工具契约清单（幂等：重复调用重启）。"""
        result = await self._request(
            "initialize", {"cwd": self._cwd}, timeout_s=self._boot_timeout_s
        )
        self._server_tools = result.get("tools", [])
        return self._server_tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """调用 DSH 工具，返回灵汐 ToolExecutionResult 同构信封。

        Returns:
            ``{success: bool, data: Any, error: str|None, duration_ms: float}``
            ——与内核 invoker 的三级响应判定兼容（有 success 字段即作信封）。
        """
        # 惰性 boot：首个调用前未 initialize（DSH context 冷启动以秒计）。
        if self._server_tools is None:
            try:
                await self.initialize()
            except (BridgeUnavailableError, RuntimeError, TimeoutError) as e:
                return {"success": False, "data": None, "error": f"dsh runtime boot failed: {e}", "duration_ms": 0.0}
        try:
            return await self._request(
                "tool/call",
                {"name": name, "args": args, "timeoutMs": int(self._call_timeout_s * 1000)},
                timeout_s=self._call_timeout_s + 10,
            )
        except BridgeUnavailableError as e:
            return {"success": False, "data": None, "error": str(e), "duration_ms": 0.0}

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._server_tools is not None:
            return self._server_tools
        return await self.initialize()

    async def shutdown(self) -> None:
        """优雅关停（on_unload / 测试收尾）。"""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await self._request("shutdown", {}, timeout_s=10)
            except Exception:  # noqa: BLE001 - 关停路径尽力而为
                pass
        await self._teardown()


# ── 模块级单例（sidecar 全部 MCP 调用共享一个 Node runtime） ────────────

_bridge: DshRuntimeBridge | None = None


def get_bridge(extra_plugins_dir: str | None = None) -> DshRuntimeBridge:
    """取（惰性创建）共享桥实例；首次创建时注入外部工具包装载区。"""
    global _bridge  # noqa: PLW0603
    if _bridge is None:
        _bridge = DshRuntimeBridge(extra_plugins_dir=extra_plugins_dir)
    return _bridge


async def shutdown_bridge() -> None:
    """关停共享桥（on_unload 钩子调用）。"""
    global _bridge  # noqa: PLW0603
    if _bridge is not None:
        await _bridge.shutdown()
        _bridge = None
