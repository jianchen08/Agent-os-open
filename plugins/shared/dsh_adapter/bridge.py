"""DSH Node runtime 桥接宿主（通道 A：npm 运行时工作区 boot）。

管理 ``runtime/dsh-rpc-bridge.mjs`` 子进程的生命周期，经 stdio 上的
newline-delimited JSON-RPC 调用 DSH 工具：

- 惰性启动：首个工具调用时 spawn Node 子进程并 ``initialize``（boot 裁剪
  cordis context，冷启动以秒计）；
- 失败重启：子进程死亡后下次调用自动重启（``_proc`` 置空，调用方无感）；
- 超时兜底：每调用独立超时（默认 120s，与 bash 工具族上限一致量级）；
- 优雅退出：``shutdown()`` 发 ``shutdown`` 帧并等进程退出，供 on_unload。

boot 落位（设计 §4.2 C2）：Node 侧从**运行时工作区**（用户空间 npm 项目，
``AGENTOS_DSH_RUNTIME_ROOT`` 指向）解析 ``@deepseek-ai/*`` 基础包与已装插件
包，不再依赖任何仓库产物路径（rc8 参考仓已降级为测试夹具）。boot 裁剪轴
（AC-R4）由 ``bootPlan`` 参数下发：``{chain, webServer, hooks, packages}``——
``chain: false`` = 空工作区不加载任何 DSH 包。

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
_RUNTIME_ROOT_ENV = "AGENTOS_DSH_RUNTIME_ROOT"
_DEFAULT_CWD = os.getcwd()

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INTERNAL = -32000

# boot 裁剪轴缺省（无 plan 参数时的协议级缺省 = 最小工具链，无外挂包）。
DEFAULT_BOOT_PLAN: dict[str, Any] = {"chain": True, "packages": []}


class BridgeUnavailableError(RuntimeError):
    """DSH runtime 不可用（工作区未配置 / Node 缺失 / 启动失败）。"""


def _background_task_done(label: str, task: asyncio.Task[None]) -> None:
    """fire-and-forget 后台任务的完成回调：异常必须留痕，禁静默丢失。

    取消属正常收尾（teardown 路径）；其余异常以 error 级日志可见。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[dsh-bridge] 后台任务 %s 异常退出: %s", label, exc, exc_info=exc)


class DshRuntimeBridge:
    """DSH 工具 Node runtime 的单进程宿主（每个 sidecar 插件实例一个）。"""

    def __init__(
        self,
        runtime_root: str | None = None,
        cwd: str | None = None,
        call_timeout_s: float = 120.0,
        boot_timeout_s: float = 60.0,
        boot_plan: dict[str, Any] | None = None,
    ) -> None:
        # runtime_root 解析序：显式参数 > 环境变量；用户空间默认解析在调用方
        # （runtime_workspace.resolve_runtime_root）。未配置时 _ensure_proc
        # fail-visible（BridgeUnavailableError 指明需设的 env）——无机器特定
        # 硬编码缺省（E2 修复契约保持）。
        self._runtime_root = runtime_root or os.environ.get(_RUNTIME_ROOT_ENV, "")
        self._cwd = cwd or _DEFAULT_CWD
        self._call_timeout_s = call_timeout_s
        self._boot_timeout_s = boot_timeout_s
        self._boot_plan = boot_plan
        self._proc: asyncio.subprocess.Process | None = None
        self._id_counter = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._server_tools: list[dict[str, Any]] | None = None
        self._web_server_origin: str | None = None
        self._web_server_error: str | None = None

    # ── 进程管理 ─────────────────────────────────────────────

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        """确保子进程存活（惰性启动 + 死亡重启）。"""
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        # 清理旧壳（returncode 非None 或 reader 残留）
        await self._teardown(quiet=True)
        if not _RUNTIME_SCRIPT.is_file():
            raise BridgeUnavailableError(f"dsh runtime script missing: {_RUNTIME_SCRIPT}")
        if not self._runtime_root:
            raise BridgeUnavailableError(
                f"DSH 运行时工作区未配置：需设置环境变量 {_RUNTIME_ROOT_ENV} 指向 npm "
                "运行时工作区（或在构造 DshRuntimeBridge 时显式传入 runtime_root）"
            )
        if not Path(self._runtime_root).is_dir():
            raise BridgeUnavailableError(
                f"DSH runtime workspace not found: {self._runtime_root} "
                f"(set {_RUNTIME_ROOT_ENV} to the user-space npm runtime workspace)"
            )
        env = {**os.environ, _RUNTIME_ROOT_ENV: self._runtime_root}
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
        # 引用保存 + 完成回调：异常经回调留痕，任务不再无引用静默丢失。
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._stderr_task.add_done_callback(
            lambda t: _background_task_done("drain_stderr", t)
        )
        return self._proc

    async def _drain_stderr(self) -> None:
        """后台排空子进程 stderr（Node 侧日志转 debug）。

        不在此吞异常：任何失败（含流读取错误）直接传播，经任务完成回调
        ``_background_task_done`` 以 error 级留痕。
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("[dsh-bridge] %s", text)

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
                fut = self._pending.pop(frame.get("id"), None)  # type: ignore[arg-type]  # frame.get 返回 Any，pop 键型按 str 收窄
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
            except asyncio.CancelledError:
                pass  # 上行刚 cancel() 本任务，等到的就是取消——teardown 预期路径
            except Exception:  # noqa: BLE001
                logger.exception("[dsh-bridge] reader task raised during teardown")
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass  # 上行刚 cancel() 本任务，等到的就是取消——teardown 预期路径
            except Exception:  # noqa: BLE001
                logger.exception("[dsh-bridge] stderr task raised during teardown")
            self._stderr_task = None
        if self._proc is not None:
            if self._proc.returncode is None:
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except TimeoutError:
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

    async def initialize(self, boot_plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """boot 裁剪 cordis context 并取工具契约清单（幂等：重复调用重启）。

        ``boot_plan``（AC-R4 裁剪轴）落 ``bootPlan`` 参数下发；显式传参覆盖
        构造期缺省（管理面启停包后以新 plan 重 boot）。
        """
        plan = boot_plan if boot_plan is not None else (self._boot_plan or DEFAULT_BOOT_PLAN)
        self._boot_plan = plan
        result = await self._request(
            "initialize", {"cwd": self._cwd, "bootPlan": plan}, timeout_s=self._boot_timeout_s
        )
        self._server_tools = result.get("tools", [])
        info = result.get("serverInfo") or {}
        self._web_server_origin = info.get("webServerOrigin") or None
        self._web_server_error = info.get("webServerError") or None
        return self._server_tools

    @property
    def web_server_origin(self) -> str | None:
        """boot 后的 webServer 本地 origin（AC-X2；未装 provider = None）。"""
        return self._web_server_origin

    @property
    def web_server_error(self) -> str | None:
        """webServer 轴不可用原因（fail-visible；可用时 None）。"""
        return self._web_server_error

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """调用 DSH 工具，返回灵汐 ToolExecutionResult 同构信封。

        Returns:
            ``{success: bool, data: Any, error: str|None, duration_ms: int}``
            ——与内核 invoker 的三级响应判定兼容（有 success 字段即作信封）；
            duration_ms 必须是整数毫秒（内核 Option<u64> 严格反序列化，
            小数位会整条判 PARSE_ERROR）。
        """
        # 惰性 boot：首个调用前未 initialize（DSH context 冷启动以秒计）。
        if self._server_tools is None:
            try:
                await self.initialize()
            except (BridgeUnavailableError, RuntimeError, TimeoutError) as e:
                return {"success": False, "data": None, "error": f"dsh runtime boot failed: {e}", "duration_ms": 0}
        try:
            return await self._request(
                "tool/call",
                {"name": name, "args": args, "timeoutMs": int(self._call_timeout_s * 1000)},
                timeout_s=self._call_timeout_s + 10,
            )
        except BridgeUnavailableError as e:
            return {"success": False, "data": None, "error": str(e), "duration_ms": 0}

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._server_tools is not None:
            return self._server_tools
        return await self.initialize()

    # ── 扩展方法（设计 §六 E1 桥协议扩展位） ─────────────────────

    async def _ensure_initialized(self) -> str | None:
        """惰性 initialize（首次调用前 boot）；失败返回错误文本（None=就绪）。"""
        if self._server_tools is not None:
            return None
        try:
            await self.initialize()
        except (BridgeUnavailableError, RuntimeError, TimeoutError) as e:
            return f"dsh runtime boot failed: {e}"
        return None

    async def call_service(
        self, name: str, method: str, args: list[Any] | None = None
    ) -> dict[str, Any]:
        """调用 DSH Service 实例方法（AC-X1），返回与 ``call_tool`` 同构信封。

        Args:
            name: boot context 内的服务名（``ctx.<name>``，如 ``webServer``）。
            method: 服务实例方法名。
            args: 位置参数数组（服务方法签名直传）。

        服务不存在/方法不存在/抛错 → ``success=False`` + error 文本（fail-visible）。
        """
        boot_err = await self._ensure_initialized()
        if boot_err is not None:
            return {"success": False, "data": None, "error": boot_err, "duration_ms": 0}
        try:
            return await self._request(
                "service/call",
                {
                    "name": name,
                    "method": method,
                    "args": list(args or []),
                    "timeoutMs": int(self._call_timeout_s * 1000),
                },
                timeout_s=self._call_timeout_s + 10,
            )
        except BridgeUnavailableError as e:
            return {"success": False, "data": None, "error": str(e), "duration_ms": 0}

    async def emit_hook(self, event: str, payload: Any = None) -> dict[str, Any]:
        """把事件送回 DSH 拦截点监听器（AC-X3）。

        Returns:
            ``{ok: bool, event: str, decision: Any, error: str|None}``——decision
            为监听器 waterfall 组合结果（无监听器 = payload 原样，调用方按
            payload is None 或无 decision 字段判定无裁决）。
        """
        boot_err = await self._ensure_initialized()
        if boot_err is not None:
            return {"ok": False, "event": event, "decision": None, "error": boot_err}
        try:
            result = await self._request(
                "hook/emit",
                {"event": event, "payload": payload if payload is not None else {}},
                timeout_s=self._call_timeout_s,
            )
            return result if isinstance(result, dict) else {
                "ok": False, "event": event, "decision": None, "error": "non-object bridge result"
            }
        except BridgeUnavailableError as e:
            return {"ok": False, "event": event, "decision": None, "error": str(e)}
        except RuntimeError as e:
            return {"ok": False, "event": event, "decision": None, "error": str(e)}

    async def http_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        """经 boot 的 webServer 监听器发请求（AC-X2 反代传输面）。

        Returns:
            ``{ok, status, headers, body_base64, error, duration_ms}``；
            webServer 轴未装/provider 缺 → ``ok=False`` + error（fail-visible）。
        """
        import base64

        boot_err = await self._ensure_initialized()
        if boot_err is not None:
            return {"ok": False, "status": 0, "headers": {}, "body_base64": "",
                    "error": boot_err, "duration_ms": 0}
        if self._web_server_origin is None and self._web_server_error:
            return {"ok": False, "status": 0, "headers": {}, "body_base64": "",
                    "error": self._web_server_error, "duration_ms": 0}
        try:
            result = await self._request(
                "http/request",
                {
                    "method": method,
                    "path": path,
                    "headers": headers or {},
                    "body_base64": base64.b64encode(body).decode("ascii") if body else "",
                },
                timeout_s=self._call_timeout_s,
            )
            return result if isinstance(result, dict) else {
                "ok": False, "status": 0, "headers": {}, "body_base64": "",
                "error": "non-object bridge result", "duration_ms": 0,
            }
        except BridgeUnavailableError as e:
            return {"ok": False, "status": 0, "headers": {}, "body_base64": "",
                    "error": str(e), "duration_ms": 0}
        except RuntimeError as e:
            return {"ok": False, "status": 0, "headers": {}, "body_base64": "",
                    "error": str(e), "duration_ms": 0}

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


def get_bridge(
    runtime_root: str | None = None, boot_plan: dict[str, Any] | None = None
) -> DshRuntimeBridge:
    """取（惰性创建）共享桥实例；首次创建时注入运行时工作区根与 boot 计划。

    ``boot_plan`` 缺省 = 按工作区实扫生成（runtime_workspace.boot_plan_for，
    设计 §4.2 裁剪轴）——服务类/webServer 类包的贡献面信号自动进轴。
    """
    global _bridge  # noqa: PLW0603
    if _bridge is None:
        from runtime_workspace import boot_plan_for, resolve_runtime_root

        root = runtime_root or str(resolve_runtime_root())
        if boot_plan is None:
            from translator import DSH_SOURCE_VERSION

            boot_plan = boot_plan_for(root, host_dsh_version=DSH_SOURCE_VERSION)
        _bridge = DshRuntimeBridge(runtime_root=root, boot_plan=boot_plan)
    return _bridge


def reset_bridge() -> None:
    """复位共享桥（单测隔离 / 管理面启停后强制重 boot）。"""
    global _bridge  # noqa: PLW0603
    _bridge = None


async def shutdown_bridge() -> None:
    """关停共享桥（on_unload 钩子调用）。"""
    global _bridge  # noqa: PLW0603
    if _bridge is not None:
        await _bridge.shutdown()
        _bridge = None
