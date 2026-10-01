"""DSH 钩子执行管道步骤（链位无关，init/prepare/post 均可放置）。

翻译端：dsh_adapter.translator.translate_hooks_config 把 DSH 声明式钩子
（{on, when?, run, timeoutMs?}）翻成 name=pipeline_dsh_hook 的步骤条目，
管道配置决定链位（init=管道启动段 / prepare=每轮前 / post=每轮后）。

参数经 per-plugin inputs 通道传入（内核组 config={"inputs": {...}}，不进
state、不落 trace）；执行结果写 state_updates["dsh_hook"] 供观测。失败
不抛异常不阻断管道（钩子是辅助步骤），但必须可见：ok=False 携带证据 +
warning 日志。

执行语义：argv 直接 exec（不经 shell 解析）、wait_for 超时杀进程、
stdout 不留存（成功只记时长），失败记 stderr 尾巴。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pipeline.plugin import IPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

_OUTPUT_TAIL_CHARS = 2000


class DshHookPlugin(IPlugin):
    """DSH 钩子执行步骤。

    参数（config["inputs"]）：
        - command: list[str]，argv 参数列表，必填，不经 shell 解析
        - timeout_ms: int，超时毫秒数（默认 10000，超时杀进程）
        - event: str，DSH 事件名标签（仅随结果回写，便于观测）
        - cwd: str，工作目录（可选）

    结果（state_updates["dsh_hook"]）：
        event / ok / exit_code / duration_ms，失败时附 error 或 stderr_tail。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "dsh_hook"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 50)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行钩子命令并回写结果（见类 docstring 契约）。"""
        return PluginResult(state_updates=await self._run(ctx.config))

    async def _run(self, config: Any) -> dict[str, Any]:
        """校验 inputs → 执行 argv → 组装可见结果（成功/失败均落 state）。"""
        inputs = config.get("inputs") if isinstance(config, dict) else None
        command, timeout_ms, error = self._validate(inputs)
        if error is not None:
            logger.warning("[dsh_hook] %s", error)
            return {"dsh_hook": {
                "ok": False, "exit_code": None, "error": error, "duration_ms": 0,
            }}
        assert command is not None
        assert timeout_ms is not None
        assert inputs is not None  # _validate 无错即 inputs 为合法 dict
        event = str(inputs.get("event") or "")
        cwd_value = inputs.get("cwd")
        cwd = str(cwd_value) if cwd_value else None
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except OSError as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.warning("[dsh_hook] spawn failed (event=%s): %s", event, exc)
            return {"dsh_hook": {
                "event": event, "ok": False, "exit_code": None,
                "error": f"spawn failed: {exc}", "duration_ms": duration,
            }}
        timed_out = False
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
        except TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        duration = int((time.monotonic() - start) * 1000)
        if timed_out:
            logger.warning("[dsh_hook] timeout after %dms (event=%s)", timeout_ms, event)
            return {"dsh_hook": {
                "event": event, "ok": False, "exit_code": None,
                "error": f"timeout after {timeout_ms}ms", "duration_ms": duration,
            }}
        exit_code = proc.returncode
        ok = exit_code == 0
        result: dict[str, Any] = {
            "event": event, "ok": ok, "exit_code": exit_code, "duration_ms": duration,
        }
        if not ok:
            result["stderr_tail"] = (stderr or b"").decode("utf-8", errors="replace")[-_OUTPUT_TAIL_CHARS:]
            logger.warning("[dsh_hook] command failed (event=%s, exit=%s)", event, exit_code)
        return {"dsh_hook": result}

    @staticmethod
    def _validate(inputs: Any) -> tuple[list[str] | None, int | None, str | None]:
        """校验 inputs，返回 (command, timeout_ms, error)；error 非 None 时前两项无意义。"""
        if not isinstance(inputs, dict):
            return None, None, "config error: inputs missing or not an object"
        command = inputs.get("command")
        if not isinstance(command, list) or not command:
            return None, None, "config error: inputs.command must be a non-empty argv list"
        if not all(isinstance(c, str) for c in command):
            return None, None, "config error: inputs.command elements must all be strings"
        raw_timeout = inputs.get("timeout_ms", 10000)
        try:
            timeout_ms = int(raw_timeout)
        except (TypeError, ValueError):
            return None, None, f"config error: inputs.timeout_ms not an integer: {raw_timeout!r}"
        if timeout_ms <= 0:
            return None, None, f"config error: inputs.timeout_ms must be positive: {timeout_ms}"
        return command, timeout_ms, None
