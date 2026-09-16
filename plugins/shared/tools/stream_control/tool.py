"""stream_control 工具：FFmpeg 推流进程面（细化设计 P2 交付，D2 角标烧录）。

- build_ffmpeg_args：纯函数构造循环播放 + drawtext 角标 + RTMP 推流参数
- StreamControlTool：start/stop/status 三动作；单推流面（重复 start 显式拒绝）；
  进程经 ProcessRunner 端口注入（测试伪件 / 生产 asyncio.subprocess）
- RTMP 地址来源：构造参数（server.py 从 AGENTOS_LIVESTREAM_RTMP 读），
  未配置时显式 RTMP_UNSET——不降级假推流。
"""

from __future__ import annotations

from typing import Any, Protocol

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

_OVERLAY_FONT_SIZE = 28
_OVERLAY_MARGIN = 16


def build_ffmpeg_args(
    *,
    inputs: list[str],
    rtmp_url: str,
    overlay_text: str,
    width: int,
    height: int,
) -> list[str]:
    """构造 FFmpeg 推流参数：多输入循环拼接 + 角标 drawtext + flv 推流。

    播放策略（D2）：concat 拼接循环（内容库无缝轮播）；
    overlay_text 为空则不烧角标（待机画面素材自身带标识时使用）。
    """
    args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
    for path in inputs:
        args += ["-stream_loop", "-1", "-i", path]
    filter_parts = [f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"]
    if overlay_text:
        filter_parts.append(
            "drawtext=text='" + overlay_text.replace("'", "") + "'"
            f":fontsize={_OVERLAY_FONT_SIZE}:fontcolor=white@0.85"
            f":box=1:boxcolor=black@0.45:boxborderw=8"
            f":x=w-tw-{_OVERLAY_MARGIN}:y={_OVERLAY_MARGIN}"
        )
    args += ["-vf", ",".join(filter_parts)]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "4500k",
             "-c:a", "aac", "-b:a", "128k", "-f", "flv", rtmp_url]
    return args


class ProcessRunner(Protocol):
    """进程端口：启动即返回句柄（terminate 语义由实现保证）。"""

    async def start(self, args: list[str]) -> Any: ...


class _SubprocessRunner:
    """生产实现：asyncio.subprocess 常驻进程。"""

    async def start(self, args: list[str]) -> Any:
        import asyncio

        return await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )


class StreamControlTool(BuiltinTool):
    """推流控制工具（直播 W1 输出面）。"""

    run_on_main_loop = True

    def __init__(self, runner: ProcessRunner | None = None, rtmp_url: str | None = None) -> None:
        self._runner: ProcessRunner = runner or _SubprocessRunner()
        self._rtmp_url = rtmp_url
        self._proc: Any = None
        self._current_inputs: list[str] = []

    @staticmethod
    def get_tool_definition() -> Tool:
        """工具定义（与 plugin.json 同源，一致性由测试护栏锁定）。"""
        return Tool(
            name="stream.control",
            description="FFmpeg 推流控制：start/stop/status；AI 角标烧录防无人直播误判（D2）",
            when_to_use=["直播开播起推流", "下播停推流", "巡检推流状态"],
            when_not_to_use=["视频转码/剪辑", "拉流播放"],
            caveats=["单推流面：已在推时 start 会被拒绝", "RTMP 地址经环境变量注入不入仓"],
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop", "status"]},
                    "inputs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "播放列表（视频文件路径，start 必填）",
                    },
                    "overlay_text": {
                        "type": "string",
                        "description": "画面角标文本（AI 生成内容标识，start 时建议必带）",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            tags=["stream", "ffmpeg", "livestream"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """分发三动作。"""
        action = str(inputs.get("action", ""))
        if action == "start":
            return await self._start(inputs)
        if action == "stop":
            return self._stop()
        if action == "status":
            return create_success_result(data=self._status())
        return create_failure_result(error=f"未知 action: {action}", error_code="BAD_ACTION")

    async def _start(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        if self._proc is not None:
            return create_failure_result(
                error="已在推流中（先 stop 再 start）", error_code="ALREADY_STREAMING"
            )
        if not self._rtmp_url:
            return create_failure_result(
                error="未配置 RTMP 地址（AGENTOS_LIVESTREAM_RTMP）", error_code="RTMP_UNSET"
            )
        inputs_list = [str(p) for p in inputs.get("inputs", []) if p]
        if not inputs_list:
            return create_failure_result(error="inputs 播放列表为空", error_code="EMPTY_PLAYLIST")
        args = build_ffmpeg_args(
            inputs=inputs_list,
            rtmp_url=self._rtmp_url,
            overlay_text=str(inputs.get("overlay_text", "")),
            width=1280,
            height=720,
        )
        self._proc = await self._runner.start(args)
        self._current_inputs = inputs_list
        return create_success_result(
            data={"running": True, "playlist": inputs_list},
            metadata={"action": "stream.control/start"},
        )

    def _stop(self) -> ToolExecutionResult:
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None
            self._current_inputs = []
        return create_success_result(data={"running": False})

    def _status(self) -> dict[str, Any]:
        return {"running": self._proc is not None, "playlist": self._current_inputs}
