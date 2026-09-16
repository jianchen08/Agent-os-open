# @feature: FP-0.2.二 视频生成 | @ci: python-coverage
"""video_gen 插件（ComfyUI 视频段生成适配层）单元测试。

覆盖（对齐 plugins/shared/tools/video_gen/comfy_client.py 与 tool.py）：
1. 模板选择：continue → i2v 续接模板 / cut → t2v 场景模板
2. 模板渲染：占位符深度替换（positive/start_image/seed/frames 等），渲染后无 {{ 残留
3. 校验：continue 缺 start_frame、空 visual_prompts、output_dir 缺失 → 显式错误
4. 提交-轮询-取片全流程（伪 transport：先未完成再完成），产出文件落盘
5. 失败换 seed 重试 ×2（共 3 次尝试），每次 seed 不同；3 次全败 → GENERATION_FAILED
6. 超时（伪时钟推进到 deadline）→ 进入重试
7. 并发串行化：并发=1 锁，第二请求在第一请求完成后才开始
8. 端点未配置 → ENDPOINT_UNSET 显式错误
9. plugin.json 与工具定义一致性（名称/schema 必填项对齐）
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).parent


def _load(name: str, filename: str, alias: str | None = None) -> Any:
    """按文件路径加载插件模块（exec 期把本目录置顶 sys.path，防合宿裸名争用）。

    alias：把模块同时挂到真实导入名上，保证 tool.py 的
    `from comfy_client import ...` 命中同一模块对象（异常类同一性）。
    """
    mod_name = f"video_gen_test_{name}"
    sys.path.insert(0, str(_PLUGIN_DIR))
    try:
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        if alias:
            sys.modules[alias] = module
    finally:
        while str(_PLUGIN_DIR) in sys.path:
            sys.path.remove(str(_PLUGIN_DIR))
    return module


_client_mod = _load("comfy_client", "comfy_client.py", alias="comfy_client")
_tool_mod = _load("tool", "tool.py")

ComfyUIClient = _client_mod.ComfyUIClient
ComfyUIError = _client_mod.ComfyUIError
VideoGenTool = _tool_mod.VideoGenTool


class FakeClock:
    """伪时钟：monotonic 可手动推进；sleep 计数不真等（时序断言用计数不用零延迟）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """伪 transport：按脚本依次返回 (status, body)；记录全部请求。"""

    def __init__(self, script: list[tuple[int, Any]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        self.calls.append((method, path))
        if not self.script:
            return 500, {"error": "script exhausted"}
        return self.script.pop(0)


def _history_entry(completed: bool, status_str: str = "success") -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    if completed and status_str == "success":
        outputs = {
            "9": {
                "videos": [
                    {"filename": "out_0001.mp4", "subfolder": "seg", "type": "output"}
                ]
            }
        }
    return {"status": {"completed": completed, "status_str": status_str}, "outputs": outputs}


def _make_client(transport: FakeTransport, clock: FakeClock) -> Any:
    return ComfyUIClient(
        base_url="http://comfy.test:8188",
        token="t0k",
        request_fn=transport,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )


def _make_tool(
    client: Any = None,
    endpoint: str | None = "http://comfy.test:8188",
    tmp: Path | None = None,
) -> Any:
    return VideoGenTool(
        endpoint=endpoint,
        token="t0k",
        client=client,
        templates_dir=_PLUGIN_DIR / "templates",
    )


def _base_inputs(tmp: Path, **overrides: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "visual_prompts": ["a cat on a roof, cinematic", "rain starts, dramatic"],
        "output_dir": str(tmp / "out"),
        "sec": 5,
    }
    inputs.update(overrides)
    return inputs


def test_continue_transition_uses_i2v_template_with_start_frame(tmp_path: Path) -> None:
    """continue：必须选 i2v 模板且 start_image 占位符被替换为 start_frame。"""
    transport = FakeTransport([
        (200, {"prompt_id": "p1"}),
        (200, {"p1": _history_entry(completed=True)}),
        (200, b"mp4bytes"),
    ])
    clock = FakeClock()
    tool = _make_tool(_make_client(transport, clock), tmp=tmp_path)
    start = tmp_path / "prev_last.jpg"
    start.write_bytes(b"jpeg")

    result = asyncio.run(tool.execute(_base_inputs(
        tmp_path,
        transition={"type": "continue"},
        start_frame=str(start),
    )))

    assert result.success, result.error
    submits = [c for c in transport.calls if c[1] == "/prompt"]
    assert len(submits) == 1
    views = [c for c in transport.calls if c[1].startswith("/view")]
    assert len(views) == 1
    data = result.output
    assert data["template"] == "i2v_continue"
    assert data["attempts"] == 1
    assert (Path(data["file_path"])).read_bytes() == b"mp4bytes"


def test_cut_transition_uses_t2v_template_without_start_image(tmp_path: Path) -> None:
    """cut：选 t2v 模板；工作流中不得出现 start_image 占位残留。"""
    seen_bodies: list[dict[str, Any]] = []

    async def transport(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        seen_bodies.append(json_body or {})
        if path == "/prompt":
            return 200, {"prompt_id": "p2"}
        if path.startswith("/history"):
            return 200, {"p2": _history_entry(completed=True)}
        return 200, b"video"

    clock = FakeClock()
    client = ComfyUIClient(
        base_url="http://x", token="t", request_fn=transport,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    tool = _make_tool(client, tmp=tmp_path)

    result = asyncio.run(tool.execute(_base_inputs(tmp_path, transition={"type": "cut"})))

    assert result.success, result.error
    assert result.output["template"] == "t2v_scene"
    workflow = seen_bodies[0]["prompt"]
    assert "{{" not in json.dumps(workflow)


def test_render_replaces_all_placeholders_deep(tmp_path: Path) -> None:
    """渲染必须替换所有占位符（deep walk），包括嵌套 inputs 内的 seed/frames。"""
    tool = _make_tool(endpoint=None, tmp=tmp_path)
    workflow = tool.render_workflow(
        "t2v_scene",
        positive_prompt="a, b",
        start_image=None,
        frames=120,
        width=1280,
        height=720,
        seed=42,
    )
    text = json.dumps(workflow)
    assert "{{" not in text
    assert "42" in text
    assert "120" in text


def test_continue_without_start_frame_is_rejected(tmp_path: Path) -> None:
    tool = _make_tool(endpoint=None, tmp=tmp_path)
    result = asyncio.run(tool.execute(_base_inputs(
        tmp_path, transition={"type": "continue"},
    )))
    assert not result.success
    assert result.error_code == "MISSING_START_FRAME"


def test_empty_visual_prompts_rejected(tmp_path: Path) -> None:
    tool = _make_tool(endpoint=None, tmp=tmp_path)
    result = asyncio.run(tool.execute(_base_inputs(tmp_path, visual_prompts=[])))
    assert not result.success
    assert result.error_code == "MISSING_PROMPTS"


def test_endpoint_unset_is_explicit_failure(tmp_path: Path) -> None:
    tool = _make_tool(endpoint=None, tmp=tmp_path)
    result = asyncio.run(tool.execute(_base_inputs(tmp_path)))
    assert not result.success
    assert result.error_code == "ENDPOINT_UNSET"


def test_retry_on_error_uses_new_seed_then_succeeds(tmp_path: Path) -> None:
    """第 1 次 status_str=error → 换 seed 重试第 2 次成功；attempts=2 且 seed 不同。"""
    seeds_seen: list[Any] = []

    async def transport(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        if path == "/prompt":
            graph = (json_body or {})["prompt"]
            seeds = [
                node["inputs"]["seed"]
                for node in graph.values()
                if isinstance(node, dict) and "seed" in node.get("inputs", {})
            ]
            seeds_seen.extend(seeds)
            return 200, {"prompt_id": f"p{len(seeds_seen)}"}
        if path.startswith("/history"):
            pid = path.rsplit("/", 1)[-1]
            n = int(pid[1:])
            if n == 1:
                return 200, {pid: _history_entry(completed=True, status_str="error")}
            return 200, {pid: _history_entry(completed=True)}
        return 200, b"vid"

    clock = FakeClock()
    client = ComfyUIClient(
        base_url="http://x", token="t", request_fn=transport,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    tool = _make_tool(client, tmp=tmp_path)

    result = asyncio.run(tool.execute(_base_inputs(tmp_path, seed=7)))

    assert result.success, result.error
    assert result.output["attempts"] == 2
    assert len(seeds_seen) == 2
    assert seeds_seen[0] == 7
    assert seeds_seen[1] != 7
def test_timeout_triggers_retry_then_exhaustion(tmp_path: Path) -> None:
    """history 永不 completed → 每次尝试超时，3 次尝试后 GENERATION_FAILED。"""
    async def transport(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        if path == "/prompt":
            return 200, {"prompt_id": f"p{len([1])}"}
        if path.startswith("/history"):
            return 200, {}
        return 200, b""  # pragma: no cover

    clock = FakeClock()
    client = ComfyUIClient(
        base_url="http://x", token="t", request_fn=transport,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    tool = _make_tool(client, tmp=tmp_path)

    result = asyncio.run(tool.execute(_base_inputs(tmp_path, timeout_secs=10, poll_interval_secs=2)))

    assert not result.success
    assert result.error_code == "GENERATION_FAILED"
    # 超时前必须真的等满（sleep 计数 = 轮询次数，禁止零延迟假等待）
    assert clock.sleeps and all(s == 2 for s in clock.sleeps)


def test_concurrent_executes_serialize(tmp_path: Path) -> None:
    """并发=1：第二个请求必须在第一个完成（取到 /view）之后才提交。"""
    transport = FakeTransport([
        (200, {"prompt_id": "a"}),
        (200, {"a": _history_entry(completed=True)}),
        (200, b"one"),
        (200, {"prompt_id": "b"}),
        (200, {"b": _history_entry(completed=True)}),
        (200, b"two"),
    ])
    clock = FakeClock()
    tool = _make_tool(_make_client(transport, clock), tmp=tmp_path)

    async def run_two() -> tuple[Any, Any]:
        return await asyncio.gather(
            tool.execute(_base_inputs(tmp_path)),
            tool.execute(_base_inputs(tmp_path)),
        )

    results = asyncio.run(run_two())
    assert all(r.success for r in results)
    submits = [i for i, c in enumerate(transport.calls) if c[1] == "/prompt"]
    views = [i for i, c in enumerate(transport.calls) if c[1].startswith("/view")]
    # 第一个的 /view 必须早于第二个的 /prompt（串行化证据）
    assert views[0] < submits[1]


def test_history_error_status_propagates_as_comfy_error() -> None:
    client = _make_client(
        FakeTransport([(200, {"p1": _history_entry(completed=True, status_str="error")})]),
        FakeClock(),
    )
    with pytest.raises(ComfyUIError):
        asyncio.run(client.wait("p1", timeout_secs=10))


def test_plugin_json_matches_tool_definition() -> None:
    """plugin.json 声明与 get_tool_definition 的名称/必填项一致（G2 同源护栏口径）。"""
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = manifest["capabilities"]["tools"][0]
    definition = VideoGenTool.get_tool_definition()
    assert declared["name"] == definition.name
    declared_required = set(declared["input_schema"].get("required", []))
    def_required = set(definition.input_schema.get("required", []))
    assert declared_required == def_required
    assert "output_schema" in declared and "render" in declared
