"""
task_submit 工具的单元测试

覆盖 inherit 参数改造场景：
1. mode 为字符串（向后兼容）: mode="pipe"
2. mode 为列表（pipe + workspace）: mode=["pipe", "workspace"]
3. mode 为非法值时返回错误

说明：
- Schema 层测试直接调用 get_tool_definition() 检查定义
- 解析层测试通过 mock 跑 execute()，让代码走到 inherit 解析后立即
  在 create_task 处失败（mock 抛异常），从 error_code 反推解析层行为
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── 把项目根加入 sys.path（保证 import core.* / tools.* / tasks.* 等正常） ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.builtin.task_submit.tool import TaskSubmitTool, _normalize_description  # noqa: E402
from tools.types import Tool  # noqa: E402

# ─────────────────────────── 工具函数 ───────────────────────────


def _make_source_task(pipeline_run_id: str = "pipe-12345") -> MagicMock:
    """构造一个 mock 源任务对象，带 ws_meta 和 pipeline_run_id。"""
    src = MagicMock()
    src.id = "src-task-001"
    src.title = "源任务"
    src.pipeline_run_id = pipeline_run_id
    src.metadata = {
        "ws_meta": {"path": "/tmp/src-ws"},
        "task_scope": "non_container",
    }
    return src


def _build_inputs(mode, inherit_from: str = "src-task-001") -> dict:
    """构造一组用于 task_submit.execute() 的最小输入。"""
    return {
        "goal": {"title": "继承任务测试"},
        "target_type": "agent",
        "target_id": "general_agent",
        "task_scope": "non_container",
        "acceptance_criteria": {"file_check": {"input_params": {"path": "src/foo.py"}}},
        "parent_agent_level": 1,
        "inherit": {
            "from": inherit_from,
            "mode": mode,
        },
    }


def _patch_infrastructure():
    """构造 mock 后的 patch 上下文管理器列表，屏蔽基础设施副作用。"""
    # mock task_service
    mock_task_service = MagicMock()
    mock_task_service.get_task.return_value = _make_source_task()
    # 让 create_task 直接抛异常，强制返回 TASK_CREATE_FAILED
    # 这样可以从 error_code 反推 inherit 解析层是否成功
    mock_task_service.create_task.side_effect = RuntimeError("stop-at-create")
    mock_task_service.bind_pipeline_run = MagicMock()
    mock_task_service.hard_delete = MagicMock()

    # mock task_worker
    mock_task_worker = MagicMock()
    mock_task_worker.submit_task.return_value = True

    # mock service provider
    mock_service_provider = MagicMock()
    mock_service_provider.get.return_value = mock_task_worker
    mock_service_provider.get_or_create.return_value = mock_task_service

    patches = [
        patch.object(TaskSubmitTool, "_get_task_service", return_value=mock_task_service),
        patch.object(
            TaskSubmitTool,
            "_validate_target_agent",
            return_value=(True, "", ""),
        ),
        patch.object(
            TaskSubmitTool,
            "_check_dependencies_exist",
            return_value=[],
        ),
        patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_service_provider,
        ),
        # 屏蔽 ws 广播副作用
        patch.dict(
            sys.modules,
            {
                "ws_handler": MagicMock(ws_interaction_notifier=MagicMock(send_to_user=MagicMock())),
            },
        ),
    ]
    return patches, mock_task_service


# ─────────────────────────── Schema 验证 ───────────────────────────


def test_inherit_mode_schema_supports_string_and_array():
    """Schema 层：inherit.mode 应支持 string（向后兼容）和 array 两种形式。

    实现方案：使用 oneOf 包含 string 和 array 两种 schema。
    """
    tool_def: Tool = TaskSubmitTool.get_tool_definition()
    inherit_schema = tool_def.input_schema["properties"]["inherit"]
    mode_schema = inherit_schema["properties"]["mode"]

    # 必须是 oneOf 形式
    assert "oneOf" in mode_schema, f"inherit.mode 缺少 oneOf 定义，实际 schema={mode_schema}"

    one_of = mode_schema["oneOf"]
    type_kinds = [opt.get("type") for opt in one_of]
    assert "string" in type_kinds, f"inherit.mode 缺少 string 分支，实际={type_kinds}"
    assert "array" in type_kinds, f"inherit.mode 缺少 array 分支，实际={type_kinds}"

    # string 分支应允许 pipe/workspace
    string_opt = next(opt for opt in one_of if opt.get("type") == "string")
    assert set(string_opt.get("enum", [])) == {"pipe", "workspace"}

    # array 分支应限定元素为 pipe/workspace
    array_opt = next(opt for opt in one_of if opt.get("type") == "array")
    items_schema = array_opt.get("items", {})
    assert items_schema.get("type") == "string"
    assert set(items_schema.get("enum", [])) == {"pipe", "workspace"}


# ─────────────────────────── 解析层验证 ───────────────────────────


def _run_execute_capture_error_code(inputs: dict) -> str:
    """运行 execute 并返回错误码。"""
    tool = TaskSubmitTool()
    patches, _ = _patch_infrastructure()

    for p in patches:
        p.start()
    try:
        result = asyncio.run(tool.execute(inputs))
    finally:
        for p in patches:
            p.stop()
    return result.error_code or ""


def test_inherit_mode_string_pipe_backward_compatible():
    """向后兼容：mode='pipe' 字符串形式应正常通过 inherit 解析层。

    验证：不应返回 INVALID_INHERIT_MODE 或 INVALID_INHERIT_PARAMS 错误。
    （最终在 create_task 处失败是因为 mock，不是 inherit 解析的问题）
    """
    inputs = _build_inputs(mode="pipe")
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code != "INVALID_INHERIT_MODE", f"mode='pipe' 字符串应被识别为合法，但收到错误码={error_code}"
    assert error_code != "INVALID_INHERIT_PARAMS", f"mode='pipe' 字符串应被识别为合法，但收到错误码={error_code}"


def test_inherit_mode_string_workspace_backward_compatible():
    """向后兼容：mode='workspace' 字符串形式应正常通过 inherit 解析层。"""
    inputs = _build_inputs(mode="workspace")
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code != "INVALID_INHERIT_MODE", f"mode='workspace' 字符串应被识别为合法，但收到错误码={error_code}"
    assert error_code != "INVALID_INHERIT_PARAMS"


def test_inherit_mode_list_pipe_and_workspace():
    """新功能：mode=['pipe', 'workspace'] 列表应同时处理两种继承。

    当前实现（改造前）会把整个 list 当作 mode 字符串处理，
    走 else 分支返回 INVALID_INHERIT_MODE 错误。改造后应通过解析层。
    """
    inputs = _build_inputs(mode=["pipe", "workspace"])
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code != "INVALID_INHERIT_MODE", (
        f"mode=['pipe', 'workspace'] 应被识别为合法，但收到错误码={error_code}。"
        "这说明 inherit.mode 解析未支持 list 形式。"
    )
    assert error_code != "INVALID_INHERIT_PARAMS", (
        f"mode=['pipe', 'workspace'] 应被识别为合法，但收到错误码={error_code}"
    )


def test_inherit_mode_list_single_pipe():
    """新功能：mode=['pipe'] 单元素列表也应工作。"""
    inputs = _build_inputs(mode=["pipe"])
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code != "INVALID_INHERIT_MODE", f"mode=['pipe'] 应被识别为合法，但收到错误码={error_code}"
    assert error_code != "INVALID_INHERIT_PARAMS"


def test_inherit_mode_list_single_workspace():
    """新功能：mode=['workspace'] 单元素列表也应工作。"""
    inputs = _build_inputs(mode=["workspace"])
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code != "INVALID_INHERIT_MODE", f"mode=['workspace'] 应被识别为合法，但收到错误_code={error_code}"
    assert error_code != "INVALID_INHERIT_PARAMS"


def test_inherit_mode_invalid_string_returns_error():
    """非法字符串值：mode='invalid' 应返回 INVALID_INHERIT_MODE 错误。"""
    inputs = _build_inputs(mode="invalid")
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code == "INVALID_INHERIT_MODE", f"mode='invalid' 应返回 INVALID_INHERIT_MODE 错误，实际={error_code}"


def test_inherit_mode_list_with_invalid_value_returns_error():
    """非法列表值：mode=['invalid'] 应返回 INVALID_INHERIT_MODE 错误。"""
    inputs = _build_inputs(mode=["invalid"])
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code == "INVALID_INHERIT_MODE", f"mode=['invalid'] 应返回 INVALID_INHERIT_MODE 错误，实际={error_code}"


def test_inherit_mode_list_with_mixed_valid_and_invalid_returns_error():
    """混合值：mode=['pipe', 'invalid'] 应返回 INVALID_INHERIT_MODE 错误。"""
    inputs = _build_inputs(mode=["pipe", "invalid"])
    error_code = _run_execute_capture_error_code(inputs)

    assert error_code == "INVALID_INHERIT_MODE", (
        f"mode=['pipe', 'invalid'] 应返回 INVALID_INHERIT_MODE 错误，实际={error_code}"
    )


# ─────────────────────────── metadata 写入验证 ───────────────────────────


def test_build_metadata_handles_list_mode_for_pipe():
    """_build_metadata 应正确处理 mode 为包含 'pipe' 的列表。

    当 mode=['pipe', 'workspace'] 时，metadata['inherit_pipe_from'] 应被设置。
    """
    tool = TaskSubmitTool()
    inputs = _build_inputs(mode=["pipe", "workspace"])
    goal = inputs["goal"]
    criteria = inputs["acceptance_criteria"]

    metadata = tool._build_metadata(inputs, goal, criteria)

    # 验证 inherit 配置被原样存储
    assert metadata.get("inherit") == inputs["inherit"]

    # 验证 pipe 标记被设置（即使 mode 是列表也应正确识别）
    assert metadata.get("inherit_pipe_from") == "src-task-001", (
        f"mode 为列表时 inherit_pipe_from 应被设置，实际 metadata={metadata}"
    )


def test_build_metadata_handles_string_mode_for_pipe():
    """_build_metadata 应正确处理 mode 为 'pipe' 字符串。"""
    tool = TaskSubmitTool()
    inputs = _build_inputs(mode="pipe")
    goal = inputs["goal"]
    criteria = inputs["acceptance_criteria"]

    metadata = tool._build_metadata(inputs, goal, criteria)

    assert metadata.get("inherit_pipe_from") == "src-task-001", (
        f"mode='pipe' 字符串时 inherit_pipe_from 应被设置，实际 metadata={metadata}"
    )


def test_build_metadata_no_pipe_mark_for_workspace_only_list():
    """_build_metadata 当 mode=['workspace'] 时不应设置 inherit_pipe_from。"""
    tool = TaskSubmitTool()
    inputs = _build_inputs(mode=["workspace"])
    goal = inputs["goal"]
    criteria = inputs["acceptance_criteria"]

    metadata = tool._build_metadata(inputs, goal, criteria)

    # workspace-only 模式不应触发 pipe 标记
    assert "inherit_pipe_from" not in metadata, (
        f"mode=['workspace'] 不应设置 inherit_pipe_from，实际 metadata={metadata}"
    )


# ─────────────────────────── description 归一化验证 ───────────────────────────


def test_normalize_description_list_to_string():
    """LLM 返回 list 形式的 description 应被归一化为 str。

    回归场景：LLM 偶尔把多行文本写成数组（如 ['在当前执行环境...', '']），
    若不归一化会静默持久化进 YAML，最终在 API 层 TaskResponse.description
    （pydantic 强制 str）校验失败导致 500。
    """
    result = _normalize_description(["在当前执行环境", ""])
    assert isinstance(result, str)
    assert result == "在当前执行环境\n", f"list 应按换行连接，实际={result!r}"


def test_normalize_description_string_passthrough():
    """正常 str 输入应原样返回。"""
    assert _normalize_description("正常描述") == "正常描述"


def test_normalize_description_empty_string():
    """空串原样返回。"""
    assert _normalize_description("") == ""


def test_normalize_description_none_to_empty():
    """None 应归一化为空串。"""
    assert _normalize_description(None) == ""


def test_normalize_description_list_with_multiple_items():
    """多元素 list 用换行连接。"""
    result = _normalize_description(["第一行", "第二行", "第三行"])
    assert result == "第一行\n第二行\n第三行"


def test_normalize_description_tuple():
    """tuple 与 list 同等处理。"""
    result = _normalize_description(("第一行", "第二行"))
    assert result == "第一行\n第二行"


def test_normalize_description_non_string_scalar():
    """非 str 标量（int/dict）转为字符串，避免 len() 校验失效。"""
    assert _normalize_description(42) == "42"
    assert _normalize_description(True) == "True"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
