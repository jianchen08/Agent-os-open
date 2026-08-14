#!/usr/bin/env python3
"""Bash 工具 MCP 服务端（0.2 sidecar 接口适配层）。

生命周期治理（修复：每次 MCP 调用新建 BashTool 导致进程状态丢失）：
- `_tool` 为模块级惰性单例：所有 MCP 调用复用同一个 BashTool/ProcessManager，
  active_processes 跨调用保留（execute → input → continue → terminate 全链路可用）。
- `on_unload` + `atexit`：sidecar 卸载/退出时调用 ProcessManager.shutdown_all()
  终止所有活动进程并取消看门狗，防止残留进程变孤儿。

本插件自包含（零 0.1 src 依赖），不注入任何 sys.path hack。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import time

from agentos_plugin_sdk import AgentOSPlugin, FrontendEmitter

from tool import BashTool

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("bash_tool")

# ── 模块级单例：跨 MCP 调用共享进程状态 ──────────────────────────
_tool: BashTool | None = None


def _get_tool() -> BashTool:
    """获取（惰性创建）共享的 BashTool 实例。

    所有 MCP 调用复用同一实例——ProcessManager 的 active_processes 因此
    在调用间存活，continue/input/terminate/read_log 凭 pid 可命中。
    """
    global _tool  # noqa: PLW0603
    if _tool is None:
        _tool = BashTool()
    return _tool


def _progress_forwarder(call_context: dict):
    """构建执行中进度推送前向器（task_observability 任务 2）。

    组装 ProgressReporter（1KB/2s 节流）+ asyncio.Queue 消费者（llm_core
    同款模式）：_read_output 每行调 report → 阈值触发 → 消费者协程经
    frontend.emit 推 tool_progress 事件（bash_execute await 期间事件循环
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
                "tool_name": "bash_execute",
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


@plugin.tool(
    name="bash_execute",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["execute", "continue", "terminate", "input", "read_log"],
                "description": "execute=执行新命令；"
                "continue=凭 pid 继续等（仍在跑则等到完成或超时，已完成则直接返回结果）；"
                "terminate=凭 pid 终止；input=凭 pid 向等待输入的进程发文本（yes/no、密码等）；"
                "read_log=凭 pid 读完整日志（任何时候都能用，进程已结束也能读）。",
                "default": "execute",
            },
            "command": {
                "type": "string",
                "description": "要执行的Shell命令（action=execute 时必需）",
            },
            "pid": {
                "type": "integer",
                "description": "进程ID（action=continue/terminate/input/read_log 时必需，由 execute 或 continue 的 running 返回值提供）",
            },
            "timeout": {
                "type": "integer",
                "description": "等待上限秒数（默认30，最大290）。超时不杀进程，返回 running+pid 供继续轮询。",
                "default": 30,
                "maximum": 290,
            },
            "working_dir": {
                "type": "string",
                "description": "命令执行的工作目录，默认为当前目录",
            },
            "input_text": {
                "type": "string",
                "description": "向运行中进程发送的输入文本（action=input 时必需）。如 yes/no、密码、菜单选项编号。",
            },
            "force": {
                "type": "boolean",
                "description": "强制终止（action=terminate 时有效，默认 false）",
                "default": False,
            },
        },
        "required": [],
    },
    description="执行 Shell 命令。不要手动 nohup/setsid/disown/行尾&（本工具自带后台执行，"
    "手动后台化会使进程脱离管理）。读文件/看目录用 file_read、搜文件内容/文件名用 enhanced_search。"
    "危险命令（rm -rf /、format、dd if= 等）会被拦截。Windows 与 Linux/Mac 语法可能不同。",
)
async def bash_execute(**kwargs):
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

    bash = _get_tool()
    try:
        result = await bash.execute(kwargs, on_output=report)
    finally:
        if finalize is not None:
            await finalize()
    if result.success:
        return result.output
    return {"error": result.error, "error_code": result.error_code}


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
