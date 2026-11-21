#!/usr/bin/env python3
"""bash 工具 MCP 服务端（0.2 sidecar 接口适配层）—— 四单词工具。

生命周期治理：
- `_tool` 为模块级惰性单例：所有 MCP 调用复用同一个 BashTool/ProcessManager，
  active_processes 跨调用保留（bash 启动 → log/input/kill 凭 handle 全链路可用）。
- `on_unload` + `atexit`：sidecar 卸载/退出时调用 ProcessManager.shutdown_all()
  终止所有活动进程并取消看门狗，防止残留进程变孤儿。

本插件自包含（零 0.1 src 依赖），不注入任何 sys.path hack。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import time
from typing import Any

from tool import BashTool

from agentos_plugin_sdk import AgentOSPlugin, FrontendEmitter

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("bash_tool")

# ── 模块级单例：跨 MCP 调用共享进程状态 ──────────────────────────
_tool: BashTool | None = None


def _get_tool() -> BashTool:
    """获取（惰性创建）共享的 BashTool 实例。

    所有 MCP 调用复用同一实例——ProcessManager 的 active_processes 因此
    在调用间存活，log/input/kill 凭 handle（内含 pid + owner）可命中。
    """
    global _tool  # noqa: PLW0603
    if _tool is None:
        _tool = BashTool()
    return _tool


def _failure_envelope(result: Any) -> dict[str, Any]:
    """工具失败信封：error/error_code 之外透传 metadata 执行痕迹。

    metadata（exit_code / terminated_by_signal）是上游区分「命令进程已
    跑完、退出码非零」与「工具本身执行不了」的唯一结构化信号——失败熔断
    据此不计连败（duplicate_check，2026-11-11 用户裁定）。
    """
    envelope: dict[str, Any] = {
        "success": False,
        "error": result.error,
        "error_code": result.error_code,
    }
    if result.metadata:
        envelope["metadata"] = result.metadata
    return envelope


def _progress_forwarder(call_context: dict):
    """构建执行中进度推送前向器（task_observability 任务 2）。

    组装 ProgressReporter（1KB/2s 节流）+ asyncio.Queue 消费者（llm_core
    同款模式）：_read_output 每行调 report → 阈值触发 → 消费者协程经
    frontend.emit 推 tool_progress 事件（bash 工具 await 期间事件循环
    空闲，消费者得以并发运行）。

    Returns:
        (report 回调, 收尾协程工厂)。frontend capability 不可用或路由键
        缺失时返回 (None, None)——进度推送优雅降级，工具照常执行。
    """
    from progress_reporter import ProgressReporter  # noqa: PLC0415

    if not call_context.get("call_id") or not call_context.get("thread_id"):
        return None, None
    emitter = FrontendEmitter.from_plugin(plugin)
    if emitter is None:
        return None, None

    call_id = str(call_context.get("call_id", ""))
    thread_id = str(call_context.get("thread_id", ""))
    pipeline_id = str(call_context.get("pipeline_id", ""))
    message_id = str(call_context.get("message_id", ""))
    started_at = time.monotonic()
    queue: asyncio.Queue[tuple[str, int] | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    async def _consumer() -> None:
        """从队列取 (delta, bytes_total) 推 tool_progress 到前端。"""
        while True:
            item = await queue.get()
            if item is None:
                break
            delta, bytes_total = item
            await emitter.emit("tool_progress", {
                "thread_id": thread_id,
                "pipeline_id": pipeline_id,
                "message_id": message_id,
                "call_id": call_id,
                "tool_name": "bash",
                "delta": delta,
                "bytes_read": bytes_total,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            })

    def _on_flush(delta: str, bytes_total: int) -> None:
        """ProgressReporter 阈值回调 → 入队（O(1)，输出读取任务安全调用）。"""
        queue.put_nowait((delta, bytes_total))

    reporter = ProgressReporter(_on_flush)
    consumer_task = loop.create_task(_consumer())

    async def _finalize() -> None:
        """工具调用结束：冲刷残留缓冲 + 哨兵终止消费者（排空不丢尾）。"""
        reporter.close()
        queue.put_nowait(None)
        try:
            await consumer_task
        except Exception:  # noqa: BLE001
            logger.debug("[bash_tool] 进度消费者收尾异常（忽略）", exc_info=True)

    return reporter.report, _finalize


def _start_run_abort_watch(call_context: Any) -> tuple[asyncio.Event | None, Any]:
    """装配并启动 run 中止看门狗 → (中止事件, 轮询任务)；不启用返回 (None, None)。

    看门狗是增强能力：装配/启动任何一步失败都降级为不启用，工具本体照常执行
    （与进度前向器同一降级哲学）。
    """
    try:
        from run_abort_watch import start_watcher  # noqa: PLC0415

        if not isinstance(call_context, dict):
            return None, None
        watcher = start_watcher(plugin, call_context)
        if watcher is None:
            return None, None
        return watcher.event, asyncio.create_task(watcher.watch())
    except Exception:  # noqa: BLE001 —— 看门狗故障不得阻断命令执行
        logger.debug("[bash_tool] run 中止看门狗装配失败（降级为不启用）", exc_info=True)
        return None, None


def _stop_run_abort_watch(task: Any) -> None:
    """取消看门狗轮询任务（调用结束收尾，幂等；任务已结束则空转）。"""
    if task is None or task.done():
        return
    task.cancel()


async def _shutdown_all() -> None:
    """终止所有活动进程并停止看门狗（sidecar 退出前的清理）。"""
    tool = _get_tool()
    try:
        await tool.process_manager.shutdown_all()
    except Exception:  # noqa: BLE001
        logger.exception("[bash_tool] shutdown_all 异常（忽略，进程即将退出）")


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """生命周期 on_unload：内核卸载插件前优雅终止全部活动进程。"""
    logger.info("[bash_tool] on_unload：终止全部活动进程")
    await _shutdown_all()


def _atexit_cleanup() -> None:
    """进程退出兜底清理（stdin EOF / SIGTERM / 异常退出时触发）。"""
    try:
        # 事件循环可能已不存在，尽力而为
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_shutdown_all())
        loop.close()
    except Exception:  # noqa: BLE001
        pass


# ── 四工具注册（声明即注册；input_schema 与 plugin.json 全等，R3 契约） ──


@plugin.tool(
    name="bash",
    schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "前台等待上限秒数（默认 30，最大 600）。超时不杀进程："
                "返回 running+handle 并附带全部已捕获输出，进程继续运行，"
                "可凭 handle 用 log/input/kill 管理。",
                "default": 30,
                "maximum": 600,
            },
            "background": {
                "type": "boolean",
                "description": "true=立即返回 handle 不等待（长任务用）；"
                "false=前台阻塞到完成或等待上限（默认 false）。",
                "default": False,
            },
            "working_dir": {
                "type": "string",
                "description": "命令执行的工作目录，默认为当前目录",
            },
        },
        "required": ["command"],
    },
    description="执行 Shell 命令。前台阻塞到完成或等待上限；超时/后台返回 running+handle，"
    "进程不死，凭 handle 用 log 读输出、input 发交互输入、kill 终止。"
    "不要手动 nohup/setsid/disown/行尾&（本工具自带 background=true，"
    "手动后台化会使进程脱离管理）。读文件/看目录用 file_read、搜文件内容/文件名用 enhanced_search。"
    "危险命令（rm -rf /、format、dd if= 等）会被拦截。Windows 与 Linux/Mac 语法可能不同。",
)
async def bash(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """执行 Shell 命令（0.2 MCP 入口）。

    所有调用共享同一个 BashTool 单例——进程状态跨调用保持。
    失败响应携带稳定 error_code（PROCESS_FORBIDDEN / PROCESS_NOT_FOUND 等），
    便于 LLM 与调用方按码分支。

    工具执行中的 stdout 增量经 frontend.emit 推 tool_progress 进度
    （task_observability 任务 2）：内核在 args 注入 _call_context 路由键，
    据此构建节流前向器；推送链路任何一环缺失都优雅降级（工具照常执行）。
    """
    # 剥离路由上下文（内核 tool-executor 合入；旧内核无此字段）
    call_context = kwargs.pop("_call_context", None) or {}
    report, finalize = (None, None)
    if isinstance(call_context, dict) and call_context.get("call_id"):
        try:
            report, finalize = _progress_forwarder(call_context)
        except Exception:  # noqa: BLE001
            logger.debug("[bash_tool] 进度前向器构建失败（降级）", exc_info=True)

    # run 中止看门狗（run_abort_watch）：run 卡在长命令里时，停止信号（run_status
    # =suspended）只能在工具返回后由引擎在 step/轮边界看到——本看门狗旁路轮询，
    # 命中即置事件，由 run_command 整树杀进程并快速返回，run 立刻回到边界收尾，
    # 排队注入消息随派发链 drain 消费。装配失败/能力缺席一律降级为不启用。
    abort_event, abort_task = _start_run_abort_watch(call_context)
    bash_tool = _get_tool()
    try:
        result = await bash_tool.run_command(kwargs, on_output=report, abort_event=abort_event)
    finally:
        _stop_run_abort_watch(abort_task)
        if finalize is not None:
            await finalize()
    if result.success:
        return result.output
    return _failure_envelope(result)


@plugin.tool(
    name="log",
    schema={
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "进程句柄（bash 工具的返回值）",
            },
            "block": {
                "type": "boolean",
                "description": "true=等到完成或等待上限（默认 30 秒）后返回；"
                "false=立即返回当前状态与完整日志（默认 false）。"
                "进程已结束后任何时候都能读（含 sidecar 重启后跨会话定位）。",
                "default": False,
            },
        },
        "required": ["handle"],
    },
    description="凭 handle 读 bash 进程日志。block=false 立即返回当前状态与完整输出"
    "（进程已结束也能读）；block=true 等到完成或等待上限。长任务超时返回 handle 后，"
    "用它轮询/取回完整输出。",
)
async def log(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """读日志（0.2 MCP 入口）：block=false 读当前态，block=true 等待完成。"""
    bash_tool = _get_tool()
    result = await bash_tool.read_log(kwargs)
    if result.success:
        return result.output
    return _failure_envelope(result)


@plugin.tool(
    name="input",
    schema={
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "进程句柄（bash 工具的返回值）",
            },
            "text": {
                "type": "string",
                "description": "要发送的输入文本，如 yes/no、密码、菜单选项编号。",
            },
        },
        "required": ["handle", "text"],
    },
    description="凭 handle 向等待输入的 bash 进程发送文本（交互式命令的 yes/no、密码、"
    "菜单选项等）。",
)
async def input(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """发送输入（0.2 MCP 入口）。"""
    bash_tool = _get_tool()
    result = await bash_tool.send_input(kwargs)
    if result.success:
        return result.output
    return _failure_envelope(result)


@plugin.tool(
    name="kill",
    schema={
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "进程句柄（bash 工具的返回值）",
            },
        },
        "required": ["handle"],
    },
    description="凭 handle 终止 bash 进程（整树杀，日志保留——kill 后 log 仍可读完整输出）。",
)
async def kill(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """终止进程（0.2 MCP 入口）：reason=agent 透传（agent 发起即 agent 语义）。"""
    bash_tool = _get_tool()
    result = await bash_tool.terminate(kwargs)
    if result.success:
        return result.output
    return _failure_envelope(result)


if __name__ == "__main__":
    atexit.register(_atexit_cleanup)
    try:
        plugin.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # stdin EOF 或异常退出时的兜底清理（atexit 之外再保险一次）
        try:
            asyncio.run(_shutdown_all())
        except Exception:  # noqa: BLE001
            pass
