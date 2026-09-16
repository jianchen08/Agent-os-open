"""video_gen 工具（ComfyUI 视频段生成适配层）。

职责（细化设计 §4.2，D8/D10）：
- 按 transition 选择模板：continue → i2v_continue（start_frame 作首帧直续）/
  cut → t2v_scene（新场景）
- 渲染 workflow 占位符 → 提交 → 轮询 → 取片落盘
- 失败换 seed 重试 ×2（共 3 次尝试）；并发=1（asyncio.Lock 串行化）
- 显性妥协：一段多镜头（shots[]）当前合并为一个正向提示词生成单条片段，
  逐镜头剪辑留 P0-T2 定栈后评估；模板图为骨架，节点连线以真机联调校准。

显式错误不降级：端点未配置（ENDPOINT_UNSET）、continue 缺 start_frame
（MISSING_START_FRAME）、空提示词（MISSING_PROMPTS）、三次尝试全败
（GENERATION_FAILED）。

裸名 import `comfy_client` 的加载路径由 server.py / 测试在 exec 期把本目录
置顶 sys.path 钉死（合宿平铺下防同名模块争用，同 media/_media_core 惯例）。
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from pathlib import Path
from typing import Any

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

from comfy_client import ComfyUIClient, ComfyUIError, _find_video

_MAX_ATTEMPTS = 3
_NEGATIVE_PROMPT = (
    "low quality, blurry, watermark, text, subtitle, deformed, extra limbs"
)


def _deep_replace(node: Any, params: dict[str, Any]) -> Any:
    """深度替换 JSON 结构中的 {{key}} 占位符。

    整串恰为单个占位符时保留值原始类型（数字/布尔不串化）；否则做子串替换。
    """
    if isinstance(node, str):
        if node.startswith("{{") and node.endswith("}}") and node[2:-2] in params:
            return params[node[2:-2]]
        out = node
        for key, value in params.items():
            token = "{{" + key + "}}"
            if token in out:
                out = out.replace(token, str(value))
        return out
    if isinstance(node, dict):
        return {k: _deep_replace(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_deep_replace(item, params) for item in node]
    return node


def _reseed_graph(workflow: dict[str, Any]) -> dict[str, Any]:
    """把工作流中所有名为 seed 的节点输入替换为新随机值。"""
    out: dict[str, Any] = {}
    for key, node in workflow.items():
        if isinstance(node, dict) and "seed" in node.get("inputs", {}):
            node = {
                **node,
                "inputs": {**node["inputs"], "seed": random.randint(0, 2**31 - 1)},
            }
        out[key] = node
    return out


class VideoGenTool(BuiltinTool):
    """视频段生成工具：ComfyUI 适配层（直播 W2 第④步的执行面）。"""

    run_on_main_loop = True

    def __init__(
        self,
        endpoint: str | None = None,
        token: str | None = None,
        client: ComfyUIClient | None = None,
        templates_dir: Path | None = None,
        fps: int = 24,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._token = token
        self._templates_dir = templates_dir or Path(__file__).parent / "templates"
        self._fps = fps
        self._lock = asyncio.Lock()

    @staticmethod
    def get_tool_definition() -> Tool:
        """工具定义（与 plugin.json 声明同源，一致性由测试护栏锁定）。"""
        return Tool(
            name="video_gen.render_segment",
            description=(
                "生成一段直播视频片段（ComfyUI）。transition=continue 时以"
                " start_frame（上一段末帧）为首帧直续；cut 时生成新场景。"
            ),
            when_to_use=["直播 LIVE 段生成", "内容库批量生产单段视频"],
            when_not_to_use=["视频剪辑/转码", "非 ComfyUI 提供方的生成"],
            caveats=[
                "单卡并发=1，多次调用自动排队",
                "失败自动换 seed 重试，最多 3 次尝试",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "visual_prompts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "画面提示词列表（英文电影感描述，将合并生成单条片段）",
                    },
                    "transition": {
                        "type": "object",
                        "description": "衔接裁决 {type: continue|cut}",
                    },
                    "start_frame": {
                        "type": "string",
                        "description": "上一段末帧图片路径（continue 时必填）",
                    },
                    "sec": {"type": "number", "description": "目标时长秒，默认 5"},
                    "width": {"type": "integer", "description": "宽，默认 1280"},
                    "height": {"type": "integer", "description": "高，默认 720"},
                    "seed": {"type": "integer", "description": "随机种子（重试自动换新）"},
                    "output_dir": {"type": "string", "description": "成片输出目录"},
                    "timeout_secs": {"type": "number", "description": "单次尝试超时秒，默认 300"},
                },
                "required": ["visual_prompts", "output_dir"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            tags=["video", "livestream", "comfyui"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行生成：校验 → 选模板渲染 → 串行提交/轮询/取片（带重试）。"""
        prompts = inputs.get("visual_prompts")
        if not isinstance(prompts, list) or not prompts or not all(
            isinstance(p, str) and p.strip() for p in prompts
        ):
            return create_failure_result(
                error="visual_prompts 必须为非空字符串列表",
                error_code="MISSING_PROMPTS",
            )
        output_dir = inputs.get("output_dir")
        if not output_dir:
            return create_failure_result(
                error="output_dir 必填", error_code="MISSING_OUTPUT_DIR"
            )
        transition = inputs.get("transition") or {"type": "cut"}
        transition_type = str(transition.get("type", "cut"))
        start_frame: str | None = inputs.get("start_frame")
        if transition_type == "continue":
            if not start_frame or not Path(str(start_frame)).is_file():
                return create_failure_result(
                    error="continue 衔接必须提供已存在的 start_frame（上一段末帧）",
                    error_code="MISSING_START_FRAME",
                )
            template_name = "i2v_continue"
        elif transition_type == "cut":
            template_name = "t2v_scene"
        else:
            return create_failure_result(
                error=f"未知 transition.type: {transition_type}",
                error_code="BAD_TRANSITION",
            )

        endpoint = self._endpoint
        if not endpoint:
            return create_failure_result(
                error="未配置 ComfyUI 端点（AGENTOS_COMFYUI_ENDPOINT）",
                error_code="ENDPOINT_UNSET",
            )

        workflow = self.render_workflow(
            template_name,
            positive_prompt=", ".join(p.strip() for p in prompts),
            start_image=start_frame if transition_type == "continue" else None,
            frames=int(float(inputs.get("sec", 5)) * self._fps),
            width=int(inputs.get("width", 1280)),
            height=int(inputs.get("height", 720)),
            seed=int(inputs.get("seed", random.randint(0, 2**31 - 1))),
        )

        client = self._client or ComfyUIClient(base_url=endpoint, token=self._token)
        timeout = float(inputs.get("timeout_secs", 300))
        poll_interval = float(inputs.get("poll_interval_secs", 2.0))

        async with self._lock:
            last_error = ""
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    prompt_id = await client.submit(workflow)
                    outputs = await client.wait(
                        prompt_id, timeout_secs=timeout, poll_interval_secs=poll_interval
                    )
                    item = _find_video(outputs)
                    if item is None:
                        raise ComfyUIError("NO_OUTPUT_FILE", str(outputs)[:500])
                    content = await client.fetch_file(item)
                    file_path = self._write_output(str(output_dir), content)
                    return create_success_result(
                        data={
                            "file_path": str(file_path),
                            "media_type": "video",
                            "template": template_name,
                            "prompt_id": prompt_id,
                            "attempts": attempt,
                        },
                        metadata={"action": "video_gen.render_segment"},
                    )
                except ComfyUIError as exc:
                    last_error = str(exc)
                    workflow = _reseed_graph(workflow)
            return create_failure_result(
                error=f"生成失败（{_MAX_ATTEMPTS} 次尝试）：{last_error}",
                error_code="GENERATION_FAILED",
            )

    def render_workflow(
        self,
        template_name: str,
        *,
        positive_prompt: str,
        start_image: str | None,
        frames: int,
        width: int,
        height: int,
        seed: int,
    ) -> dict[str, Any]:
        """加载模板并深度替换占位符（渲染后不得残留 {{）。"""
        path = self._templates_dir / f"{template_name}.json"
        template = json.loads(path.read_text(encoding="utf-8"))
        params: dict[str, Any] = {
            "positive_prompt": positive_prompt,
            "negative_prompt": _NEGATIVE_PROMPT,
            "start_image": start_image or "",
            "frames": frames,
            "fps": self._fps,
            "width": width,
            "height": height,
            "seed": seed,
        }
        return _deep_replace(template, params)

    @staticmethod
    def _write_output(output_dir: str, content: bytes) -> Path:
        """成片落盘（uuid 文件名防撞）。"""
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"seg_{uuid.uuid4().hex[:12]}.mp4"
        path.write_bytes(content)
        return path
