"""DSH Node runtime 桥接宿主（通道 A：DSH profile 运行时工作区 boot）。

管理 ``runtime/dsh-rpc-bridge.mjs`` 子进程的生命周期，经 stdio 上的
newline-delimited JSON-RPC 调用 DSH 工具与官方 plugin-manager：

- 惰性启动：首个工具调用时 spawn Node 子进程并 ``initialize``（boot 裁剪
  cordis context，冷启动以秒计）；
- 失败重启：子进程死亡后下次调用自动重启（``_proc`` 置空，调用方无感）；
- 超时兜底：每调用独立超时（默认 120s，与 bash 工具族上限一致量级）；
- 优雅退出：``shutdown()`` 发 ``shutdown`` 帧并等进程退出，供 on_unload。

boot 落位（设计 §4.2 C2）：Node 侧从**运行时工作区**（用户空间 DSH profile，
``AGENTOS_DSH_RUNTIME_ROOT`` 指向）解析 ``@deepseek-ai/*`` 基础包与已装
bundle，不再依赖任何仓库产物路径（rc8 参考仓已降级为测试夹具）。boot 计划
（AC-R4）由 ``bootPlan`` 参数下发：``source="profile"`` 走官方 bundle patch
composition，``fallback`` 是旧工作区扫描结果——``chain: false`` = 空工具链。

管理面（2026-11-09 起）：安装/卸载/启停委托官方 ``@deepseek-ai/
dsh-plugin-manager``（见 ``manager_*`` 方法）；变更返回 ``restart-required``，
调用方用 ``restart_bridge()`` 让下次工具调用重新 boot。

协议契约与 mjs 侧对齐（见 runtime/dsh-rpc-bridge.mjs 头注释）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RUNTIME_SCRIPT = Path(__file__).parent / "runtime" / "dsh-rpc-bridge.mjs"
_RUNTIME_ROOT_ENV = "AGENTOS_DSH_RUNTIME_ROOT"
_DEFAULT_CWD = os.getcwd()
# stdout 单帧上限：initialize 返回全部工具契约（含 schema），社区插件多时
# 远超 asyncio 默认 64KiB 行上限。
_STREAM_LINE_LIMIT = 16 * 1024 * 1024

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INTERNAL = -32000

# boot 计划缺省（无 plan 参数时的协议级缺省 = profile composition，空集回退最小链）。
DEFAULT_BOOT_PLAN: dict[str, Any] = {
    "source": "profile",
    "fallback": {"chain": True, "packages": []},
}


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
        manager_config: dict[str, Any] | None = None,
    ) -> None:
        # runtime_root 解析序：显式参数 > 环境变量；用户空间默认解析在调用方
        # （runtime_workspace.resolve_runtime_root）。未配置时 _ensure_proc
        # fail-visible（BridgeUnavailableError 指明需设的 env）——无机器特定
        # 硬编码缺省（E2 修复契约保持）。归一成绝对路径：Node 侧官方 profile
        # loader（readProfileManifest 等）拒绝相对路径。
        root = runtime_root or os.environ.get(_RUNTIME_ROOT_ENV, "")
        self._runtime_root = os.path.abspath(root) if root else ""
        self._cwd = os.path.abspath(cwd) if cwd else _DEFAULT_CWD
        self._call_timeout_s = call_timeout_s
        self._boot_timeout_s = boot_timeout_s
        self._boot_plan = boot_plan
        self._manager_config = manager_config or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._id_counter = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._server_tools: list[dict[str, Any]] | None = None
        self._web_server_origin: str | None = None
        self._web_server_error: str | None = None
        self._manager_ready = False
        self._manager_error: str | None = None
        self._bundle_states: list[dict[str, Any]] = []
        self._provider_states: list[dict[str, Any]] = []
        # 垫片四：Node→Python 反向 RPC 的宿主 handler 表（loop.status/stop/inject）
        # 三键内置注册（任务 d66b3780c7a9）：不再缺省为空——内核 caller 未接时
        # handler 也 fail-visible（"kernel caller not wired"），不再是
        # "no agentos handler"。handler 返回值可为同步值或 awaitable
        # （_spawn_upstream_handler 双路径收）
        self._agentos_handlers: dict[str, Callable[..., Any]] = {
            "loop.status": self._handle_loop_status,
            "loop.stop": self._handle_loop_stop,
            "loop.inject": self._handle_loop_inject,
        }
        # 内核 capability caller（server.py on_load 经 _wire_kernel_caller 注入；
        # None = 未接内核，handler 调用 fail-visible）
        self._kernel_caller: Callable[..., Any] | None = None
        # 上行请求处理任务引用集（防 GC；完成自移除）
        self._upstream_tasks: set[asyncio.Task[None]] = set()

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
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "node",
                str(_RUNTIME_SCRIPT),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # 单帧 JSON-RPC 可能很大（initialize 返回全部工具契约 + schema，
                # 社区插件 17+ 工具时超 asyncio 默认 64KiB 行上限 → 读循环
                # LimitOverrunError 把整个桥判死）。上限抬到 16MiB。
                limit=_STREAM_LINE_LIMIT,
            )
        except FileNotFoundError as exc:
            raise BridgeUnavailableError(
                "node_missing: 未找到 Node（DSH 运行时要求 Node >= 22）"
            ) from exc
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
                if isinstance(frame.get("method"), str) and "result" not in frame and "error" not in frame:
                    # Node→Python 上行请求（垫片四反向 RPC agentos/call）：分流
                    # 异步处理，响应经 _write_frame 写回；不占 _pending（下行请求
                    # id 为 int，上行请求 id 为 "up-N" 字符串，命名空间天然隔离）。
                    self._spawn_upstream_handler(frame)
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
        params: dict[str, Any] = {"cwd": self._cwd, "bootPlan": plan}
        if self._manager_config:
            params["manager"] = self._manager_config
        result = await self._request(
            "initialize", params, timeout_s=self._boot_timeout_s
        )
        self._server_tools = result.get("tools", [])
        info = result.get("serverInfo") or {}
        self._web_server_origin = info.get("webServerOrigin") or None
        self._web_server_error = info.get("webServerError") or None
        self._manager_ready = bool(info.get("managerReady"))
        self._manager_error = info.get("managerError") or None
        states = info.get("bundleStates")
        self._bundle_states = [s for s in states if isinstance(s, dict)] if isinstance(states, list) else []
        providers = info.get("providerStates")
        self._provider_states = [s for s in providers if isinstance(s, dict)] if isinstance(providers, list) else []
        return self._server_tools

    @property
    def web_server_origin(self) -> str | None:
        """boot 后的 webServer 本地 origin（AC-X2；未装 provider = None）。"""
        return self._web_server_origin

    @property
    def web_server_error(self) -> str | None:
        """webServer 轴不可用原因（fail-visible；可用时 None）。"""
        return self._web_server_error

    @property
    def manager_ready(self) -> bool:
        """官方 plugin-manager 是否已挂载（bootstrap 缺失时为 False）。"""
        return self._manager_ready

    @property
    def manager_error(self) -> str | None:
        """plugin-manager 不可用原因（fail-visible；可用时 None）。"""
        return self._manager_error

    @property
    def bundle_states(self) -> list[dict[str, Any]]:
        """profile 各 bundle 行的 cordis Fiber 状态（boot 后可见；诊断未激活原因）。"""
        return self._bundle_states

    @property
    def provider_states(self) -> list[dict[str, Any]]:
        """可选宿主服务提供包的挂载态（timer/storage/llm/…；未安装不出现）。"""
        return self._provider_states

    # ── 垫片四：Node→Python 反向 RPC（agentos/call） ──────────────

    def register_agentos_handler(self, method: str, handler: Callable[..., Any]) -> None:
        """注册 Node 桥反向调用的宿主 handler（loop.status/stop/inject）。

        幂等：同名重复注册以最后一次为准。handler 按位置实参调用
        （Node 侧服务代理把 args 数组原样展开）；同步/异步 handler 均可。
        """
        self._agentos_handlers[method] = handler

    def _write_frame(self, payload: dict[str, Any]) -> None:
        """向子进程写一帧 JSON-RPC（上行响应写回；管道死时留痕不抛）。

        write 同步入缓冲；drain 需要事件循环，从读循环上下文 create_task。
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            logger.warning("[dsh-bridge] 上行响应丢弃（进程已死）: id=%s", payload.get("id"))
            return
        stdin = proc.stdin  # 闭包内保持 None 收窄（属性收窄不进嵌套函数）
        try:
            stdin.write(f"{json.dumps(payload, ensure_ascii=False)}\n".encode())
        except Exception as e:  # noqa: BLE001 — 写失败=管道死，进程级错误已在读循环 EOF 路径处理
            logger.warning("[dsh-bridge] 上行响应写失败 id=%s: %s", payload.get("id"), e)
            return

        async def _drain() -> None:
            try:
                await stdin.drain()
            except Exception as e:  # noqa: BLE001
                logger.warning("[dsh-bridge] 上行响应 drain 失败 id=%s: %s", payload.get("id"), e)

        try:
            task = asyncio.get_running_loop().create_task(_drain())
            task.add_done_callback(self._upstream_tasks.discard)
            self._upstream_tasks.add(task)
        except RuntimeError:
            pass  # 无运行中事件循环：缓冲已写入（测试同步路径），drain 由后续 IO 推进

    def _spawn_upstream_handler(self, frame: dict[str, Any]) -> None:
        """分发 Node 桥上行请求帧（agentos/call）到宿主 handler 表。

        fail-visible：未注册 handler / handler 异常都翻译成 ``ok=False``
        错误帧回传（Node 侧服务代理按 error 原样抛出）——禁止静默丢弃。
        """
        req_id = frame.get("id")
        params = frame.get("params")
        method = params.get("method") if isinstance(params, dict) else None
        raw_args = params.get("args") if isinstance(params, dict) else None
        args: list[Any] = raw_args if isinstance(raw_args, list) else []

        async def _run() -> None:
            handler = self._agentos_handlers.get(method) if isinstance(method, str) else None
            if handler is None:
                self._write_frame({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"ok": False, "data": None,
                               "error": f"no agentos handler: {method}"},
                })
                return
            try:
                data = handler(*args)
                if asyncio.iscoroutine(data):
                    data = await data
                self._write_frame({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"ok": True, "data": data, "error": None},
                })
            except Exception as e:  # noqa: BLE001 — 边界翻译成错误帧（handler 异常不让桥死）
                self._write_frame({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}"},
                })

        task = asyncio.get_running_loop().create_task(_run())
        task.add_done_callback(self._upstream_tasks.discard)
        self._upstream_tasks.add(task)

    # ── 垫片四：宿主侧真实 loop handler（任务 d66b3780c7a9） ──────────────
    # 三个操作的内核 wire 契约（已对内核源码逐行核实）：
    #   status → service-registry.pipeline-runs.list_by_pipeline（runs 快照投影，
    #            RunRecord.status serde lowercase）；
    #   stop   → pipeline-executor.suspend_pipeline（按管道挂起最新非终态 run，
    #            幂等：无匹配 run 返回 run_id=""）；
    #   inject → chat.send_message（pipeline_id 非空即注入分支，与前端同一
    #            WS 派发链 dispatch_user_input；required: message + user_id）。
    # caller 未装配（_wire_kernel_caller 未跑 / 旧内核能力缺席）→ fail-visible
    # RuntimeError，经 _spawn_upstream_handler 翻译成 ok=False 错误帧回 Node。

    def set_kernel_caller(self, caller: Callable[..., Any] | None) -> None:
        """注入内核 capability caller（``caller(method, params) -> result``）。

        同步/异步 callable 均可（``_kernel_call`` 双路径收）；None = 断开内核
        接线（handler 调用 fail-visible）。server.py on_load 经
        ``_wire_kernel_caller`` 装配 SDK 能力句柄的统一 caller。
        """
        self._kernel_caller = caller

    async def _kernel_call(self, method: str, params: dict[str, Any]) -> Any:
        """经内核 caller 调一条 capability wire 契约（同步/异步双路径）。"""
        caller = self._kernel_caller
        if caller is None:
            raise RuntimeError(
                "kernel caller not wired (loop handler 需 on_load 的 _wire_kernel_caller 装配)")
        result = caller(method, params)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def _handle_loop_status(self, task_id: str, *_opts: Any) -> dict[str, Any]:
        """loop.status：查询管道运行状态（内核 runs 快照投影）。

        task_id = AgentOS pipeline_id（strip + lowercase 归一）；无运行行 →
        idle 形态（exists=False），不是错误。内核失败原样传播（fail-visible）。
        """
        pipeline_id = str(task_id).strip().lower()
        if not pipeline_id:
            raise ValueError("loop.status: task_id 必须非空（AgentOS pipeline_id）")
        rows = await self._kernel_call(
            "service-registry.pipeline-runs.list_by_pipeline",
            {"pipeline_id": pipeline_id},
        )
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if row is None:
            return {"pipeline_id": pipeline_id, "exists": False,
                    "run_status": None, "run_id": None}
        return {
            "pipeline_id": pipeline_id,
            "exists": True,
            "run_status": row.get("status"),
            "run_id": row.get("run_id"),
            "started_at": row.get("created_at"),
        }

    async def _handle_loop_stop(self, task_id: str, *_opts: Any) -> dict[str, Any]:
        """loop.stop：停止管道运行（内核 suspend_pipeline，内核侧幂等）。"""
        pipeline_id = str(task_id).strip()
        if not pipeline_id:
            raise ValueError("loop.stop: task_id 必须非空（AgentOS pipeline_id）")
        result = await self._kernel_call(
            "pipeline-executor.suspend_pipeline", {"pipeline_id": pipeline_id},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"loop.stop: 内核返回非对象形态: {result!r}")
        return {
            "ok": True,
            "pipeline_id": pipeline_id,
            "run_status": "suspended",
            "run_id": result.get("run_id", ""),
        }

    async def _handle_loop_inject(self, task_id: str, payload: Any = None,
                                  *_opts: Any) -> dict[str, Any]:
        """loop.inject：注入用户消息（内核 chat.send_message 注入分支）。

        payload str 原样注入；dict/list 序列化成 JSON 文本（DSH 侧结构化
        additionalContext 场景）；空载荷拒绝（不派发空消息轮）。
        """
        pipeline_id = str(task_id).strip()
        if not pipeline_id:
            raise ValueError("loop.inject: task_id 必须非空（AgentOS pipeline_id）")
        if payload is None or (isinstance(payload, str) and not payload.strip()):
            raise ValueError("loop.inject: payload 必须非空（消息文本或结构化载荷）")
        message = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        result = await self._kernel_call(
            "chat.send_message",
            {"pipeline_id": pipeline_id, "message": message,
             "user_id": "agentos-loop-inject"},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"loop.inject: 内核返回非对象形态: {result!r}")
        return {
            "ok": True,
            "injected": True,
            "pipeline_id": pipeline_id,
            "status": result.get("status"),
        }

    # ── 官方 plugin-manager 管理面 ─────────────────────────────

    async def manager_call(self, method: str, params: dict[str, Any] | None = None,
                           timeout_s: float | None = None) -> dict[str, Any]:
        """调用官方 manager RPC，返回 ``{ok, data, error}`` 信封。

        manager 未就绪 / 桥不可用 / 超时 → ``ok=False`` + error 文本；
        官方 ``ChangeResult`` 原样放在 ``data``。
        """
        boot_err = await self._ensure_initialized()
        if boot_err is not None:
            return {"ok": False, "data": None, "error": boot_err}
        try:
            result = await self._request(
                f"plugin/manager/{method}", params or {},
                timeout_s=timeout_s if timeout_s is not None else self._call_timeout_s,
            )
        except TimeoutError:
            return {"ok": False, "data": None,
                    "error": f"manager {method} timeout after {timeout_s}s"}
        except (BridgeUnavailableError, RuntimeError) as exc:
            return {"ok": False, "data": None, "error": str(exc)}
        if not isinstance(result, dict):
            return {"ok": False, "data": None, "error": "non-object manager result"}
        return result

    async def manager_call_oneshot(self, method: str, params: dict[str, Any] | None = None,
                                   timeout_s: float = 900.0) -> dict[str, Any]:
        """停常驻桥后在一次性 node 进程里执行官方 manager 变更方法。

        Windows 上 pnpm 要重命名 ``node_modules/<pkg>``，常驻桥持有包内文件句柄
        会 ``ERR_PNPM_EPERM``（dev 实测 2026-11-09）——装/卸前先停桥，再用
        ``--manager-once`` 模式（只挂官方 manager，不 boot 工具链/profile bundle）
        跑变更；下一次调用按需重建常驻桥。

        Returns:
            与 :meth:`manager_call` 同构的 ``{ok, data, error}`` 信封。
        """
        await self.shutdown()
        if not self._runtime_root:
            return {"ok": False, "data": None,
                    "error": f"DSH 运行时工作区未配置：需设置环境变量 {_RUNTIME_ROOT_ENV}"}
        request = {
            "cwd": self._cwd,
            "manager": self._manager_config,
            "method": f"plugin/manager/{method}",
            "params": params or {},
        }
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="dsh-manager-once-", delete=False, encoding="utf-8")
        try:
            json.dump(request, tmp, ensure_ascii=False)
            tmp.close()
            env = {**os.environ, _RUNTIME_ROOT_ENV: self._runtime_root}
            try:
                proc = await asyncio.create_subprocess_exec(
                    "node", str(_RUNTIME_SCRIPT), "--manager-once", tmp.name,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    limit=_STREAM_LINE_LIMIT,
                )
            except FileNotFoundError:
                return {"ok": False, "data": None,
                        "error": "node_missing: 未找到 Node（DSH 运行时要求 Node >= 22）"}
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return {"ok": False, "data": None,
                        "error": f"manager {method} oneshot timeout after {timeout_s}s"}
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        stderr_text = (err or b"").decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.debug("[dsh-bridge oneshot] %s", stderr_text[-2000:])
        lines = [line for line in (out or b"").decode("utf-8", errors="replace").splitlines() if line.strip()]
        if not lines:
            tail = stderr_text[-400:]
            return {"ok": False, "data": None,
                    "error": f"manager {method} oneshot produced no result (exit {proc.returncode}): {tail}"}
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"ok": False, "data": None,
                    "error": f"manager {method} oneshot bad result: {lines[-1][:200]}"}
        if not isinstance(result, dict):
            return {"ok": False, "data": None, "error": "non-object manager result"}
        return result

    async def manager_install_isolated(self, spec: str, request_id: str,
                                       options: dict[str, Any] | None = None,
                                       timeout_s: float = 900.0) -> dict[str, Any]:
        """装包走一次性进程（停桥后再跑官方 installBundle，防 pnpm EPERM）。"""
        return await self.manager_call_oneshot(
            "install",
            {"spec": spec, "requestId": request_id, "options": options or {}},
            timeout_s=timeout_s,
        )

    async def manager_remove_isolated(self, name: str,
                                      timeout_s: float = 900.0) -> dict[str, Any]:
        """卸包走一次性进程（同 EPERM 防护）。"""
        return await self.manager_call_oneshot("remove", {"name": name}, timeout_s=timeout_s)

    async def manager_list(self) -> dict[str, Any]:
        return await self.manager_call("list")

    async def manager_inspect(self, spec: str) -> dict[str, Any]:
        return await self.manager_call("inspect", {"spec": spec})

    async def manager_install(self, spec: str, request_id: str,
                              options: dict[str, Any] | None = None,
                              timeout_s: float = 900.0) -> dict[str, Any]:
        payload = {"spec": spec, "requestId": request_id, "options": options or {}}
        return await self.manager_call("install", payload, timeout_s=timeout_s)

    async def manager_cancel(self, request_id: str) -> dict[str, Any]:
        return await self.manager_call("cancel", {"requestId": request_id})

    async def manager_remove(self, name: str) -> dict[str, Any]:
        return await self.manager_call("remove", {"name": name})

    async def manager_set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        return await self.manager_call("enable" if enabled else "disable", {"name": name})

    async def manager_add_host_deps(self, specs: list[str],
                                    timeout_s: float = 1800.0) -> dict[str, Any]:
        """补装宿主依赖（复用官方 ``runProfilePnpm`` 最小安装链路）。

        Args:
            specs: 精确 ``name@version`` 列表（空列表直接返回 ok）。
            timeout_s: RPC 上限；Node 侧官方实现另有输出静默超时（默认 900s
                无输出即中止），总时长可达更长——本上限只防 RPC 挂死。

        Returns:
            ``{ok, data: PackageResult, error}``；不写 profile bundles。
        """
        if not specs:
            return {"ok": True, "data": {"skipped": True, "exitCode": 0}, "error": None}
        boot_err = await self._ensure_initialized()
        if boot_err is not None:
            return {"ok": False, "data": None, "error": boot_err}
        try:
            result = await self._request("plugin/host-dep/add", {"specs": list(specs)},
                                         timeout_s=timeout_s)
        except TimeoutError:
            return {"ok": False, "data": None,
                    "error": f"host dep add timeout after {timeout_s}s"}
        except (BridgeUnavailableError, RuntimeError) as exc:
            return {"ok": False, "data": None, "error": str(exc)}
        if not isinstance(result, dict):
            return {"ok": False, "data": None, "error": "non-object host dep result"}
        return result

    async def manager_registries(self) -> dict[str, Any]:
        return await self.manager_call("registries")

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

    async def emit_hook(
        self,
        event: str,
        payload: Any = None,
        *,
        exec: dict[str, Any] | None = None,
        result: Any = None,
        agent_key: str | None = None,
    ) -> dict[str, Any]:
        """把事件送回 DSH 拦截点监听器（AC-X3；垫片二/三载荷扩展）。

        ``exec``/``result``/``agent_key`` 为 tools/* 三参签名的载荷透传
        （桥侧 dsh-compat-shims.dispatchHook 按事件词表展开 + 身份物化）；
        不传时帧上不出现对应键——既有词表边界事件消费面（turn/end 等）
        保持旧形态，零回归。

        Returns:
            ``{ok: bool, event: str, decision: Any, error: str|None}``——decision
            为监听器 waterfall 组合结果（无监听器 = payload 原样，调用方按
            payload is None 或无 decision 字段判定无裁决）。
        """
        boot_err = await self._ensure_initialized()
        if boot_err is not None:
            return {"ok": False, "event": event, "decision": None, "error": boot_err}
        params: dict[str, Any] = {"event": event, "payload": payload if payload is not None else {}}
        if exec is not None:
            params["exec"] = exec
        if result is not None:
            params["result"] = result
        if agent_key:
            params["agentKey"] = agent_key
        try:
            result_payload = await self._request(
                "hook/emit",
                params,
                timeout_s=self._call_timeout_s,
            )
            return result_payload if isinstance(result_payload, dict) else {
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
    runtime_root: str | None = None,
    boot_plan: dict[str, Any] | None = None,
    manager_config: dict[str, Any] | None = None,
) -> DshRuntimeBridge:
    """取（惰性创建）共享桥实例；首次创建时注入工作区根与 boot 计划。

    ``boot_plan`` 缺省 = profile composition + 旧工作区扫描回退
    （runtime_workspace.profile_boot_plan，设计 §4.2 裁剪轴）。
    ``manager_config`` 是官方 plugin-manager 的 pnpm/registry 配置。
    """
    global _bridge  # noqa: PLW0603
    if _bridge is None:
        from runtime_workspace import profile_boot_plan, resolve_runtime_root

        root = runtime_root or str(resolve_runtime_root())
        if boot_plan is None:
            from translator import DSH_SOURCE_VERSION

            boot_plan = profile_boot_plan(root, host_dsh_version=DSH_SOURCE_VERSION)
        _bridge = DshRuntimeBridge(
            runtime_root=root, boot_plan=boot_plan, manager_config=manager_config)
    elif manager_config:
        _bridge._manager_config = manager_config  # noqa: SLF001 — 单例配置注入面
    return _bridge


def reset_bridge() -> None:
    """复位共享桥（单测隔离 / 管理面启停后强制重 boot）。"""
    global _bridge  # noqa: PLW0603
    _bridge = None


async def restart_bridge() -> None:
    """关停并复位共享桥：管理面变更后让下次工具调用重新 boot。"""
    await shutdown_bridge()


async def shutdown_bridge() -> None:
    """关停共享桥（on_unload 钩子调用）。"""
    global _bridge  # noqa: PLW0603
    if _bridge is not None:
        await _bridge.shutdown()
        _bridge = None
