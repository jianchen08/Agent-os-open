"""测试 human_interaction 工具注入链改造后的正确性。

验证项：
1. ToolDefinition 的 injected_params 只包含 pipeline_id
2. HumanInteractionTool 无参实例化正常
3. pipeline_id 注入后 execute 不报缺少上下文
4. 缺少 pipeline_id 时返回失败
5. ParamInjectPlugin 能注入 pipeline_id
6. YAML 配置与代码定义一致
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

import pytest


class TestHumanInteractionToolDefinition:
    """验证工具定义的 injected_params 只包含 pipeline_id。"""

    def test_injected_params_only_pipeline_id(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool_def = HumanInteractionTool.get_tool_definition()
        assert tool_def.injected_params == ["pipeline_id"], (
            f"Expected ['pipeline_id'], got {tool_def.injected_params}"
        )

    def test_tool_name_is_human_interaction(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool_def = HumanInteractionTool.get_tool_definition()
        assert tool_def.name == "human_interaction"

    def test_injected_params_not_contain_session_id(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool_def = HumanInteractionTool.get_tool_definition()
        assert "session_id" not in tool_def.injected_params
        assert "user_id" not in tool_def.injected_params
        assert "thread_id" not in tool_def.injected_params
        assert "tab_id" not in tool_def.injected_params
        assert "agent_id" not in tool_def.injected_params


class TestHumanInteractionToolInstantiation:
    """验证工具实例化行为。"""

    def test_no_arg_instantiation(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool = HumanInteractionTool()
        assert tool.pipeline_id is None

    def test_with_pipeline_id(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool = HumanInteractionTool(pipeline_id="pipeline-123")
        assert tool.pipeline_id == "pipeline-123"


class TestHumanInteractionToolExecution:
    """验证工具执行时 pipeline_id 的校验逻辑。"""

    @pytest.mark.asyncio
    async def test_execute_without_pipeline_id_returns_failure(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool = HumanInteractionTool()
        result = await tool.execute({"mode": "choice", "title": "Test"})
        assert result.success is False
        assert "pipeline_id" in result.error

    @pytest.mark.asyncio
    async def test_execute_with_pipeline_id_in_inputs(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool = HumanInteractionTool(pipeline_id="pipe-001")
        result = await tool.execute({
            "mode": "choice",
            "title": "Test",
            "pipeline_id": "pipe-001",
        })
        assert result.success is True or "pipeline_id" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_pipeline_id_from_inputs_not_constructor(self) -> None:
        from tools.builtin.human_interaction import HumanInteractionTool

        tool = HumanInteractionTool()
        result = await tool.execute({
            "mode": "choice",
            "title": "Test",
            "pipeline_id": "pipe-from-inputs",
        })
        assert result.success is True or "pipeline_id" not in (result.error or "")


class TestParamInjectPipelineId:
    """验证 ParamInjectPlugin 能注入 pipeline_id。"""

    @pytest.mark.asyncio
    async def test_pipeline_id_injected_from_state(self) -> None:
        from plugins.input.param_inject import ParamInjectPlugin

        plugin = ParamInjectPlugin()

        class MockContext:
            state = {
                "core_type": "tool_execute",
                "raw_tool_calls": [
                    {"name": "human_interaction", "args": {"mode": "choice", "title": "Test"}},
                ],
                "session_id": "sess-001",
                "pipeline_id": "pipe-001",
            }

            def get_service(self, name: str) -> Any:
                raise KeyError(name)

        ctx = MockContext()
        result = await plugin.execute(ctx)
        updated_calls = result.state_updates.get("raw_tool_calls", [])
        assert len(updated_calls) == 1
        assert updated_calls[0]["args"]["pipeline_id"] == "pipe-001"

    @pytest.mark.asyncio
    async def test_pipeline_id_not_overwritten_if_exists(self) -> None:
        from plugins.input.param_inject import ParamInjectPlugin

        plugin = ParamInjectPlugin()

        class MockContext:
            state = {
                "core_type": "tool_execute",
                "raw_tool_calls": [
                    {"name": "test_tool", "args": {"pipeline_id": "existing-pipe"}},
                ],
                "pipeline_id": "pipe-001",
            }

            def get_service(self, name: str) -> Any:
                raise KeyError(name)

        ctx = MockContext()
        result = await plugin.execute(ctx)
        updated_calls = result.state_updates.get("raw_tool_calls", [])
        assert updated_calls[0]["args"]["pipeline_id"] == "existing-pipe"

    @pytest.mark.asyncio
    async def test_no_pipeline_id_in_state_skips_injection(self) -> None:
        from plugins.input.param_inject import ParamInjectPlugin

        plugin = ParamInjectPlugin()

        class MockContext:
            state = {
                "core_type": "tool_execute",
                "raw_tool_calls": [
                    {"name": "test_tool", "args": {}},
                ],
            }

            def get_service(self, name: str) -> Any:
                raise KeyError(name)

        ctx = MockContext()
        result = await plugin.execute(ctx)
        updated_calls = result.state_updates.get("raw_tool_calls", [])
        assert "pipeline_id" not in updated_calls[0]["args"]


class TestYAMLConfigConsistency:
    """验证 YAML 配置与代码定义一致。"""

    def test_yaml_injected_params_match_code(self) -> None:
        import yaml

        yaml_path = _PROJECT_ROOT / "config" / "tools" / "system" / "human_interaction.yaml"
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        yaml_params = [p["name"] for p in data.get("injected_params", [])]
        from tools.builtin.human_interaction import HumanInteractionTool

        code_params = HumanInteractionTool.get_tool_definition().injected_params
        assert yaml_params == code_params, (
            f"YAML injected_params {yaml_params} != code injected_params {code_params}"
        )


class TestInjectedParamValidator:
    """验证 pipeline_id 在已知注入来源中。"""

    def test_pipeline_id_in_known_sources(self) -> None:
        from plugins.input.injected_param_validator import _KNOWN_INJECT_SOURCES

        assert "pipeline_id" in _KNOWN_INJECT_SOURCES
        assert _KNOWN_INJECT_SOURCES["pipeline_id"] == "ParamInjectPlugin"
