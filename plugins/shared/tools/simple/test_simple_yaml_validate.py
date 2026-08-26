# @feature: FP-0.2.二 内部模块 manifest | @ci: none-local
"""yaml_validate / read_execution_detail 边缘分支补测（覆盖率 A5.3）。

既有 test_system_tools.py 覆盖主路径，本文件补齐：
- yaml_validate：content 优先级 / 文件不存在 / 读取失败 / 空参 / 语法错误 /
  非对象 / schema_type 三分支（ui_scene/agent/workflow）与 required_fields 错误汇总；
- read_execution_detail：参数校验 / 三种 level 的每条降级分支 / 渲染函数边界
  （trace 的 patch_data 解析三形态、message_lines 空 preview、L1 压缩块渲染、
  L0 截断与可选字段）。
模块经 importlib 显式路径 + 唯一模块名加载（与 test_system_tools.py 同款），
唯一外部依赖为注入的 _capability_caller（AsyncMock）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

MOD_NAME = "system_tools_yaml_edge_test"


def _load_module() -> Any:
    """每次重建模块，隔离 _capability_caller 全局状态。"""
    if MOD_NAME in sys.modules:
        del sys.modules[MOD_NAME]
    spec = importlib.util.spec_from_file_location(MOD_NAME, _PLUGIN_DIR / "system_tools.py")
    assert spec is not None and spec.loader is not None, "Cannot load system_tools.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    module = _load_module()
    module._capability_caller = None
    return module


def _make_message(
    seq: int,
    role: str,
    content_preview: str = "",
    tool_calls_json: str | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    return {
        "message_id": f"msg-{seq}",
        "run_id": "run-1",
        "branch_id": "main",
        "seq_in_branch": seq,
        "role": role,
        "content_preview": content_preview,
        "pipeline_id": "pipe-1",
        "tool_calls_json": tool_calls_json,
        "tool_call_id": "call-1" if tool_calls_json else None,
        "reasoning_content": reasoning,
        "created_at": "2026-08-11T00:00:00Z",
    }


# ═══════════════════════════════════════════════════════════
# yaml_validate 纯函数
# ═══════════════════════════════════════════════════════════


class TestYamlValidateContentPrecedence:
    async def test_content_preferred_over_file_path(self, mod: Any, tmp_path: Path) -> None:
        """content 与 file_path 同时给出时 content 优先，file_path 不生效。"""
        bad = tmp_path / "bad.yaml"
        bad.write_text("x: 1", encoding="utf-8")
        result = await mod.yaml_validate(content="a: 1", file_path=str(bad))
        assert result["valid"] is True
        assert result["parsed"] == {"a": 1}

    async def test_content_empty_string_falls_to_file_path(self, mod: Any, tmp_path: Path) -> None:
        """content 为空字符串（falsy）时回退读取 file_path。"""
        good = tmp_path / "good.yaml"
        good.write_text("k: v", encoding="utf-8")
        result = await mod.yaml_validate(content="", file_path=str(good))
        assert result["valid"] is True
        assert result["parsed"] == {"k": "v"}

    async def test_file_path_only_invalid_yaml(self, mod: Any, tmp_path: Path) -> None:
        """file_path 内容为非法 YAML 时返回语法错误而非文件错误。"""
        bad = tmp_path / "bad.yaml"
        bad.write_text("a: [1,", encoding="utf-8")
        result = await mod.yaml_validate(file_path=str(bad))
        assert result["valid"] is False
        assert "YAML" in result["error"]


class TestYamlValidateFileErrors:
    async def test_file_not_exist(self, mod: Any) -> None:
        """file_path 不存在：报「文件不存在」且不带 errors 列表。"""
        result = await mod.yaml_validate(file_path="C:/nonexistent/nope.yaml")
        assert result == {"valid": False, "error": "文件不存在: C:/nonexistent/nope.yaml"}

    async def test_file_read_error(self, mod: Any, tmp_path: Path) -> None:
        """读取失败（目录当文件）返回读取失败错误。"""
        d = tmp_path / "adir"
        d.mkdir()
        result = await mod.yaml_validate(file_path=str(d))
        assert result["valid"] is False
        assert "读取文件失败" in result["error"]

    async def test_no_content_no_path(self, mod: Any) -> None:
        """content 与 file_path 均缺省：参数错误。"""
        result = await mod.yaml_validate()
        assert result == {"valid": False, "error": "必须提供 content 或 file_path"}


class TestYamlValidateContent:
    async def test_invalid_yaml_syntax(self, mod: Any) -> None:
        result = await mod.yaml_validate(content="a: 1\n- b")
        assert result["valid"] is False
        assert "YAML 语法错误" in result["error"]

    async def test_yaml_scalar_not_dict(self, mod: Any) -> None:
        """YAML 合法但非对象（标量/列表）→ 类型错误。"""
        for content in ("just-a-string", "- 1\n- 2"):
            result = await mod.yaml_validate(content=content)
            assert result["valid"] is False
            assert "必须是对象/字典类型" in result["error"]

    async def test_valid_with_required_fields(self, mod: Any) -> None:
        result = await mod.yaml_validate(content="name: x\nage: 1", required_fields=["name", "age"])
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["parsed"] == {"name": "x", "age": 1}

    async def test_missing_required_fields_accumulate(self, mod: Any) -> None:
        """多个必需字段缺失时全部汇入 errors 列表。"""
        result = await mod.yaml_validate(content="other: 1", required_fields=["name", "age", "tag"])
        assert result["valid"] is False
        assert result["errors"] == ["缺少必需字段: name", "缺少必需字段: age", "缺少必需字段: tag"]


class TestYamlSchemaTypes:
    async def test_ui_schema_requires_scene_fields(self, mod: Any) -> None:
        ok = await mod.yaml_validate(content="scene_id: s1\ndisplay_name: 场景", schema_type="ui_scene")
        assert ok["valid"] is True
        bad = await mod.yaml_validate(content="scene_id: s1", schema_type="ui_scene")
        assert bad["valid"] is False
        assert any("display_name" in e for e in bad["errors"])

    async def test_agent_requires_name(self, mod: Any) -> None:
        ok = await mod.yaml_validate(content="name: a1", schema_type="agent")
        assert ok["valid"] is True
        bad = await mod.yaml_validate(content="other: 1", schema_type="agent")
        assert bad["valid"] is False
        assert "name" in bad["errors"][0]

    async def test_workflow_requires_name(self, mod: Any) -> None:
        ok = await mod.yaml_validate(content="name: w1", schema_type="workflow")
        assert ok["valid"] is True
        bad = await mod.yaml_validate(content="x: 1", schema_type="workflow")
        assert bad["valid"] is False
        assert "name" in bad["errors"][0]

    async def test_schema_type_missing_and_unknown_both_generic(self, mod: Any) -> None:
        """缺省与未知 schema_type 均按 generic 处理，不强制字段。"""
        r1 = await mod.yaml_validate(content="a: 1")
        r2 = await mod.yaml_validate(content="a: 1", schema_type="bogus")
        assert r1["valid"] is True
        assert r2["valid"] is True


# ═══════════════════════════════════════════════════════════
# read_execution_detail 参数校验 + 降级分支
# ═══════════════════════════════════════════════════════════


class TestReadDetailParamChecks:
    async def test_missing_pipeline_run_id(self, mod: Any) -> None:
        caller = AsyncMock()
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="", level="skeleton")
        assert result == {"error": "pipeline_run_id 不能为空"}
        caller.assert_not_awaited()

    async def test_missing_level(self, mod: Any) -> None:
        caller = AsyncMock()
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="")
        assert result == {"error": "level 不能为空"}
        caller.assert_not_awaited()

    async def test_unsupported_level(self, mod: Any) -> None:
        caller = AsyncMock()
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L9")
        assert result == {"error": "不支持的 level: L9"}
        caller.assert_not_awaited()

    async def test_thread_id_kwarg_used_for_traces(self, mod: Any) -> None:
        """skeleton 的 traces.list 用 kwargs 传入的 thread_id，messages 用 pipeline_run_id。"""
        caller = AsyncMock()
        caller.return_value = []
        mod.set_capability_caller(caller)
        await mod.read_execution_detail(
            pipeline_run_id="p1", level="skeleton", thread_id="th-9"
        )
        assert caller.await_count == 2
        traces_call = caller.await_args_list[0]
        assert traces_call.args[0] == "traces.list"
        assert traces_call.args[1] == {"thread_id": "th-9"}


class TestFetchDegradation:
    async def test_messages_call_exception_degrades(self, mod: Any) -> None:
        caller = AsyncMock(side_effect=RuntimeError("kernel down"))
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L0")
        assert result["error"] == "内核 messages.list 调用失败: kernel down"

    async def test_traces_call_exception_degrades_to_empty(self, mod: Any) -> None:
        """traces.list 抛异常 → 空轨迹，skeleton 仍正常渲染。"""
        caller = AsyncMock()
        caller.side_effect = [RuntimeError("no traces"), []]  # traces 抛错, messages 空
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        assert result["level"] == "skeleton"
        assert result["trace_count"] == 0
        assert result["message_count"] == 0

    async def test_messages_list_returned_in_wrapped_dict(self, mod: Any) -> None:
        """messages.list 返回 {messages: [...]} 包裹形态时正常解包。"""
        caller = AsyncMock()
        caller.side_effect = [
            {"traces": [{"plugin_id": "p", "seq_in_branch": 1}]},
            {"messages": [_make_message(1, "user", "hi")]},
        ]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        assert result["trace_count"] == 1
        assert result["message_count"] == 1
        assert result["message_lines"] == ["[seq 1] user: hi"]

    async def test_messages_list_scalar_return_empty(self, mod: Any) -> None:
        """messages.list 返回非 list/dict 形态 → 按空列表处理。"""
        caller = AsyncMock()
        caller.side_effect = [[], "garbage"]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        assert result["message_count"] == 0

    async def test_skeleton_messages_error_dict_resets_to_empty(self, mod: Any) -> None:
        """skeleton 下 messages 失败 → 骨架只渲染轨迹，不抛错。"""
        caller = AsyncMock()
        caller.side_effect = [[], RuntimeError("boom")]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        assert result["level"] == "skeleton"
        assert result["message_count"] == 0
        assert result["message_lines"] == []

    async def test_skeleton_traces_scalar_result_degrades(self, mod: Any) -> None:
        """traces.list 返回非 list/dict 形态 → 按空轨迹处理，不崩溃。"""
        caller = AsyncMock()
        caller.side_effect = ["garbage", []]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        assert result["trace_count"] == 0
        assert result["message_count"] == 0

    async def test_l1_messages_error_dict_propagates(self, mod: Any) -> None:
        """L1 无压缩块且 messages 调用失败 → 原样返回降级错误 dict。"""
        caller = AsyncMock()
        caller.side_effect = [{"results": []}, RuntimeError("boom")]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L1")
        assert result == {"error": "内核 messages.list 调用失败: boom"}

    async def test_l1_hindsight_exception_degrades_to_messages(self, mod: Any) -> None:
        """hindsight.recall 抛异常 → 压缩块为空，降级用 messages 轮次摘要。"""
        caller = AsyncMock()
        caller.side_effect = [RuntimeError("hindsight down"), [_make_message(1, "user", "q1")]]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L1")
        assert result["level"] == "L1"
        assert result["turn_count"] == 1

    async def test_l1_hindsight_non_dict_result_degrades(self, mod: Any) -> None:
        """hindsight.recall 返回非 dict 形态（如 list）→ 空压缩块 → 降级。"""
        caller = AsyncMock()
        caller.side_effect = [[{"x": 1}], [_make_message(1, "user", "q")]]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L1")
        assert result["level"] == "L1"
        assert result["turn_count"] == 1

    async def test_l0_messages_error_dict_propagates(self, mod: Any) -> None:
        caller = AsyncMock(side_effect=RuntimeError("boom"))
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L0")
        assert result == {"error": "内核 messages.list 调用失败: boom"}


# ═══════════════════════════════════════════════════════════
# 渲染层边界
# ═══════════════════════════════════════════════════════════


class TestSkeletonRenderEdges:
    async def test_patch_data_forms(self, mod: Any) -> None:
        """patch_data 三种形态：JSON 字符串 / dict / 非法 JSON 字符串。"""
        import json as _json

        caller = AsyncMock()
        caller.side_effect = [
            [
                {"plugin_id": "a", "seq_in_branch": 1, "patch_data": _json.dumps({"core_type": "x", "llm_usage": 3})},
                {"plugin_id": "b", "seq_in_branch": 2, "patch_data": {"track.total_tokens": 9}},
                {"plugin_id": "c", "seq_in_branch": 3, "patch_data": "{not json"},
            ],
            [],
        ]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        steps = result["trace_steps"]
        assert len(steps) == 3
        # JSON 字符串解析出 key_changes
        assert steps[0]["key_changes"]["core_type"] == "x"
        # dict 直用
        assert steps[1]["key_changes"]["track.total_tokens"] == 9
        # 非法 JSON → 落入 _raw 状态键（不在白名单键内，key_changes 为 None）
        assert steps[2]["state_keys"] == ["_raw"]
        assert steps[2]["key_changes"] is None

    async def test_patch_data_missing_fields(self, mod: Any) -> None:
        """trace 缺 plugin_id / seq / patch_data → 占位渲染且 key_changes 为 None。"""
        caller = AsyncMock()
        caller.side_effect = [[{"patch_data": "{}"}], []]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        step = result["trace_steps"][0]
        assert step["plugin"] == "?"
        assert step["seq"] is None
        assert step["key_changes"] is None

    async def test_message_lines_empty_preview_renders_bare(self, mod: Any) -> None:
        """消息 content_preview 为空 → 不输出冒号后内容。"""
        caller = AsyncMock()
        caller.side_effect = [[], [_make_message(1, "user", ""), _make_message(2, "tool", "")]]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="skeleton")
        assert result["message_lines"] == ["[seq 1] user", "[seq 2] tool"]


class TestL1ChunkRender:
    async def test_l1_from_chunks(self, mod: Any) -> None:
        """L1 有压缩块：结构化 JSON content 与纯文本 content 两种渲染。"""
        import json as _json

        caller = AsyncMock()
        caller.return_value = {
            "results": [
                {
                    "content": _json.dumps({"l1": "过程", "l2": ["三元组"], "state_snapshot": {"k": 1}}),
                    "metadata": {"layer": "L1", "keywords": ["kw"], "tags": ["layer:L1", "seq:1-3"]},
                },
                {
                    "content": "纯文本摘要",
                    "metadata": {},
                },
                {
                    "content": _json.dumps({"l1": "无元数据"}),
                    "metadata": None,
                },
            ]
        }
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L1")
        assert result["level"] == "L1"
        assert result["source"] == "compression_chunks"
        assert result["chunk_count"] == 3
        chunks = result["chunks"]
        # 结构化块：无 content_preview，带 l1/l2/state_snapshot
        assert chunks[0]["content_preview"] is None
        assert chunks[0]["l1"] == "过程"
        assert chunks[0]["l2"] == ["三元组"]
        assert chunks[0]["state_snapshot"] == {"k": 1}
        assert chunks[0]["layer"] == "L1"
        assert chunks[0]["seq_range"] == "1-3"
        # 非 JSON content：content_preview 保留
        assert chunks[1]["content_preview"] == "纯文本摘要"
        assert chunks[1]["layer"] == ""
        # metadata None → 空字典兜底
        assert chunks[2]["content_preview"] is None

    async def test_l1_with_iteration_bounds(self, mod: Any) -> None:
        """L1 轮次过滤：越界报错，命中框定单轮。"""
        messages = [
            _make_message(1, "user", "q1"),
            _make_message(2, "assistant", "a1"),
            _make_message(3, "user", "q2"),
        ]
        caller = AsyncMock()
        caller.side_effect = [{"results": []}, messages]
        mod.set_capability_caller(caller)
        out_of_range = await mod.read_execution_detail(pipeline_run_id="p1", level="L1", iteration=5)
        assert "未找到 iteration=5" in out_of_range["error"]

        caller.side_effect = [{"results": []}, messages]
        hit = await mod.read_execution_detail(pipeline_run_id="p1", level="L1", iteration=2)
        assert hit["turn_count"] == 1
        # 框定后重新从 1 编号，轮次内容即原第 2 轮（q2）
        assert hit["turns"][0]["seq_range"] == [3, 3]
        assert hit["turns"][0]["user_inputs"][0]["content_preview"] == "q2"

    async def test_l1_turn_fields(self, mod: Any) -> None:
        """轮次渲染字段：seq_range / user_inputs / ai_actions / tool_calls_count。"""
        messages = [
            _make_message(1, "user", "第一问"),
            _make_message(2, "assistant", "第一答", reasoning="思考中"),
            _make_message(3, "tool", "结果"),
        ]
        caller = AsyncMock()
        caller.side_effect = [{"results": []}, messages]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L1")
        turn = result["turns"][0]
        assert turn["seq_range"] == [1, 3]
        assert turn["user_inputs"][0]["content_preview"] == "第一问"
        assert turn["ai_actions"][0]["content_preview"] == "第一答"
        assert turn["ai_actions"][0]["reasoning_preview"] == "思考中"
        assert turn["tool_calls_count"] == 1


class TestL0Render:
    async def test_l0_field_selection_and_truncation(self, mod: Any) -> None:
        """L0：截断超长内容、跳过空 tool_calls_json/tool_call_id/reasoning。"""
        long = "x" * 600
        caller = AsyncMock()
        caller.return_value = [
            _make_message(1, "assistant", long, tool_calls_json='[{"id":"c1"}]', reasoning="r" * 400),
            _make_message(2, "tool", ""),
        ]
        mod.set_capability_caller(caller)
        result = await mod.read_execution_detail(pipeline_run_id="p1", level="L0")
        rec = result["records"][0]
        assert rec["content"] == "x" * 500 + "...(truncated, total 600 chars)"
        assert rec["tool_calls_json"] == '[{"id":"c1"}]'
        assert rec["tool_call_id"] == "call-1"
        assert rec["reasoning_content"] == "r" * 400
        assert result["record_count"] == 2
        # 第二条空消息不携带可选字段
        assert "tool_calls_json" not in result["records"][1]
        assert "reasoning_content" not in result["records"][1]

    async def test_l0_iteration_filter(self, mod: Any) -> None:
        """L0 iteration 框定轮次；越界返回错误 dict。"""
        messages = [
            _make_message(1, "user", "q1"),
            _make_message(2, "assistant", "a1"),
        ]
        caller = AsyncMock(return_value=messages)
        mod.set_capability_caller(caller)
        one = await mod.read_execution_detail(pipeline_run_id="p1", level="L0", iteration=1)
        assert one["record_count"] == 2
        bad = await mod.read_execution_detail(pipeline_run_id="p1", level="L0", iteration=3)
        assert "未找到 iteration=3" in bad["error"]


class TestTruncateText:
    def test_truncate_behavior(self, mod: Any) -> None:
        """截断边界：短文本原样、长文本带计数、空值原样。"""
        assert mod._truncate_text("abc", 5) == "abc"
        assert mod._truncate_text("abcdef", 3) == "abc...(truncated, total 6 chars)"
        assert mod._truncate_text(None, 3) is None
        assert mod._truncate_text("", 3) == ""

    def test_safe_content_to_str(self, mod: Any) -> None:
        """非字符串内容 JSON 序列化；不可序列化对象回退 str()。"""
        assert mod._safe_content_to_str("plain") == "plain"
        assert mod._safe_content_to_str(None) == ""
        assert mod._safe_content_to_str({"a": 1}) == '{"a": 1}'
        assert mod._safe_content_to_str(object()) != ""
