"""
task_submit inherit 参数单元测试

测试要点：
1. schema 定义：inherit 参数存在，properties 含 from(string) 和 mode(enum:pipe/workspace)
2. inherit_workspace_from 标记废弃（description 含废弃提示）
3. inherit.mode=workspace 时复用 inherit_workspace_from 逻辑
4. inherit.mode=pipe 时记录 metadata
5. inherit 优先于 inherit_workspace_from
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.builtin.task_submit.tool import TaskSubmitTool


# ── 常量 ──

# get_service_provider 在 execute 方法内部通过
#   from infrastructure.service_provider import get_service_provider
# 导入，因此 patch 目标必须是 infrastructure.service_provider.get_service_provider
_PROVIDER_TARGET = "infrastructure.service_provider.get_service_provider"


# ── 辅助函数 ──


def _get_schema() -> dict:
    """获取 task_submit 工具的 input_schema。"""
    tool_def = TaskSubmitTool.get_tool_definition()
    return tool_def.input_schema


def _make_minimal_inputs(**overrides) -> dict:
    """构造 execute 所需的最小合法输入，通过 overrides 覆盖/追加字段。"""
    base = {
        "goal": {"title": "测试任务"},
        "target_type": "agent",
        "target_id": "test_agent",
        "acceptance_criteria": {"file_check": {"input_params": {"path": "test.txt"}}},
        "parent_agent_level": 1,
    }
    base.update(overrides)
    return base


def _build_mock_provider(
    *,
    old_task: MagicMock | None = None,
    new_task: MagicMock | None = None,
) -> MagicMock:
    """构建 mock provider，包含 TaskService / TaskWorker / AgentRegistry / Lifecycle。"""
    if new_task is None:
        new_task = MagicMock()
        new_task.id = "task_mock_001"
        new_task.title = "测试任务"
        new_task.status.value = "pending"
        new_task.metadata = {}

    mock_task_service = MagicMock()
    mock_task_service.create_task = AsyncMock(return_value=new_task)
    mock_task_service.get_task.return_value = new_task
    mock_task_service.hard_delete = AsyncMock()

    mock_task_worker = MagicMock()
    mock_task_worker.submit_task.return_value = True

    mock_agent_config = MagicMock()
    mock_agent_config.level.value = "L2"

    # 工作空间生命周期 mock：on_task_start 写入 ws_meta 到 new_task.metadata
    mock_lifecycle = MagicMock()

    def _on_task_start(task_id, workspace, task_data):  # noqa: ANN001
        new_task.metadata = new_task.metadata or {}
        new_task.metadata["ws_meta"] = {"path": f"/tmp/ws_{task_id}", "mode": "plain"}
        return new_task.metadata["ws_meta"]

    mock_lifecycle.on_task_start.side_effect = _on_task_start

    def provider_get(key):
        if key == "task_worker":
            return mock_task_worker
        if key == "agent_registry":
            reg = MagicMock()
            reg.get.return_value = mock_agent_config
            return reg
        if key == "task_service":
            return mock_task_service
        if key == "workspace_lifecycle_manager":
            return mock_lifecycle
        return None

    mock_provider = MagicMock()
    mock_provider.get_or_create.return_value = mock_task_service
    mock_provider.get.side_effect = provider_get

    return mock_provider


async def _run_execute(tool: TaskSubmitTool, inputs: dict) -> any:
    """执行 task_submit（带全套 mock），返回结果。"""
    mock_provider = _build_mock_provider()
    with (
        patch(_PROVIDER_TARGET, return_value=mock_provider),
        patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
    ):
        return await tool.execute(inputs)


# ══════════════════════════════════════════════════════════
# 一、Schema 定义测试
# ══════════════════════════════════════════════════════════


class TestInheritSchema:
    """验证 inherit 参数在 schema 中的定义符合规范。"""

    def test_schema_has_inherit_property(self):
        """schema 应包含 inherit 顶级参数。"""
        schema = _get_schema()
        assert "inherit" in schema["properties"], "schema.properties 中应包含 inherit"

    def test_inherit_is_object_type(self):
        """inherit 应为 object 类型。"""
        schema = _get_schema()
        inherit_def = schema["properties"]["inherit"]
        assert inherit_def["type"] == "object"

    def test_inherit_has_from_string_property(self):
        """inherit.properties 应包含 from（type=string）。"""
        schema = _get_schema()
        inherit_def = schema["properties"]["inherit"]
        assert "from" in inherit_def["properties"]
        assert inherit_def["properties"]["from"]["type"] == "string"

    def test_inherit_has_mode_enum_property(self):
        """inherit.properties 应包含 mode（enum=[pipe, workspace]）。"""
        schema = _get_schema()
        inherit_def = schema["properties"]["inherit"]
        assert "mode" in inherit_def["properties"]
        mode_def = inherit_def["properties"]["mode"]
        assert mode_def["type"] == "string"
        assert set(mode_def["enum"]) == {"pipe", "workspace"}

    def test_inherit_required_fields(self):
        """inherit.required 应包含 from 和 mode。"""
        schema = _get_schema()
        inherit_def = schema["properties"]["inherit"]
        assert "required" in inherit_def
        assert set(inherit_def["required"]) == {"from", "mode"}

    def test_inherit_not_in_global_required(self):
        """inherit 不应在全局 required 中（它是可选参数）。"""
        schema = _get_schema()
        global_required = schema.get("required", [])
        assert "inherit" not in global_required


class TestInheritWorkspaceFromDeprecated:
    """验证 inherit_workspace_from 已标记为废弃。"""

    def test_inherit_workspace_from_has_deprecation_hint(self):
        """inherit_workspace_from 的 description 应包含废弃提示。"""
        schema = _get_schema()
        ws_from_def = schema["properties"]["inherit_workspace_from"]
        description = ws_from_def.get("description", "")
        assert "已废弃" in description or "废弃" in description, (
            f"inherit_workspace_from description 应包含废弃提示，实际: {description}"
        )

    def test_inherit_workspace_from_mentions_inherit(self):
        """inherit_workspace_from 的 description 应引导用户使用 inherit。"""
        schema = _get_schema()
        ws_from_def = schema["properties"]["inherit_workspace_from"]
        description = ws_from_def.get("description", "")
        assert "inherit" in description, (
            f"inherit_workspace_from description 应提及 inherit，实际: {description}"
        )


# ══════════════════════════════════════════════════════════
# 二、inherit.mode=workspace 复用 inherit_workspace_from 逻辑
# ══════════════════════════════════════════════════════════


class TestInheritWorkspaceMode:
    """验证 inherit.mode=workspace 复用 inherit_workspace_from 逻辑。"""

    @pytest.mark.asyncio
    async def test_workspace_mode_triggers_task_lookup(self):
        """inherit.mode=workspace 应通过 get_task 查找源任务的 workspace。

        验证方式：让 get_task 返回有 ws_meta 的 mock 任务，确认继承成功。
        """
        tool = TaskSubmitTool()
        source_task_id = "task_source_ws_001"
        mock_ws_path = "/tmp/workspace/source_ws"

        mock_old_task = MagicMock()
        mock_old_task.metadata = {"ws_meta": {"path": mock_ws_path}}

        mock_provider = _build_mock_provider(old_task=mock_old_task)

        inputs = _make_minimal_inputs(
            inherit={"from": source_task_id, "mode": "workspace"},
        )

        with (
            patch(_PROVIDER_TARGET, return_value=mock_provider),
            patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
        ):
            result = await tool.execute(inputs)

        # 不应返回 inherit 相关错误
        assert result.error_code not in ("INVALID_INHERIT_MODE", "INVALID_INHERIT_PARAMS")
        # get_task 应被调用来查找源任务
        task_service = mock_provider.get_or_create.return_value
        task_service.get_task.assert_any_call(source_task_id)


# ══════════════════════════════════════════════════════════
# 三、inherit.mode=pipe 记录 metadata
# ══════════════════════════════════════════════════════════


class TestInheritPipeMode:
    """验证 inherit.mode=pipe 时正确记录 metadata。"""

    def test_build_metadata_stores_inherit_config_for_pipe(self):
        """_build_metadata 应将 inherit 配置存入 metadata，并记录 inherit_pipe_from。"""
        tool = TaskSubmitTool()
        inputs = {
            "inherit": {"from": "task_pipe_001", "mode": "pipe"},
            "session_id": "sess_001",
        }
        goal = {"title": "测试任务"}
        acceptance_criteria = {"file_check": {"input_params": {"path": "test.txt"}}}

        metadata = tool._build_metadata(inputs, goal, acceptance_criteria)

        assert metadata["inherit"] == {"from": "task_pipe_001", "mode": "pipe"}
        assert metadata["inherit_pipe_from"] == "task_pipe_001"

    def test_build_metadata_stores_inherit_config_for_workspace(self):
        """_build_metadata 对 workspace 模式存储 inherit 但不存储 inherit_pipe_from。"""
        tool = TaskSubmitTool()
        inputs = {
            "inherit": {"from": "task_ws_001", "mode": "workspace"},
            "session_id": "sess_001",
        }
        goal = {"title": "测试任务"}
        acceptance_criteria = {"file_check": {"input_params": {"path": "test.txt"}}}

        metadata = tool._build_metadata(inputs, goal, acceptance_criteria)

        assert metadata["inherit"] == {"from": "task_ws_001", "mode": "workspace"}
        assert "inherit_pipe_from" not in metadata

    def test_build_metadata_no_inherit(self):
        """无 inherit 参数时 metadata 不应包含 inherit 相关字段。"""
        tool = TaskSubmitTool()
        inputs = {"session_id": "sess_001"}
        goal = {"title": "测试任务"}
        acceptance_criteria = {"file_check": {"input_params": {"path": "test.txt"}}}

        metadata = tool._build_metadata(inputs, goal, acceptance_criteria)

        assert "inherit" not in metadata
        assert "inherit_pipe_from" not in metadata


# ══════════════════════════════════════════════════════════
# 四、inherit 优先于 inherit_workspace_from
# ══════════════════════════════════════════════════════════


class TestInheritPriority:
    """验证 inherit 参数优先于 inherit_workspace_from。"""

    @pytest.mark.asyncio
    async def test_inherit_from_overrides_inherit_workspace_from(self):
        """同时传 inherit 和 inherit_workspace_from 时，使用 inherit.from 的值。

        设置 inherit.mode=workspace + from=A，inherit_workspace_from=B，
        应使用 A（inherit 的值）而非 B。
        """
        tool = TaskSubmitTool()
        source_task_id = "task_inherit_A"

        mock_ws_path = "/tmp/workspace/from_inherit"
        mock_old_task = MagicMock()
        mock_old_task.metadata = {"ws_meta": {"path": mock_ws_path}}

        mock_provider = _build_mock_provider(old_task=mock_old_task)

        inputs = _make_minimal_inputs(
            inherit={"from": source_task_id, "mode": "workspace"},
            inherit_workspace_from="task_old_B",
        )

        with (
            patch(_PROVIDER_TARGET, return_value=mock_provider),
            patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
        ):
            result = await tool.execute(inputs)

        # 不应返回 inherit 相关错误
        assert result.error_code not in ("INVALID_INHERIT_PARAMS", "INVALID_INHERIT_MODE")
        # get_task 应被调用查找 "task_inherit_A"（inherit.from），而非 "task_old_B"
        task_service = mock_provider.get_or_create.return_value
        called_ids = [c.args[0] for c in task_service.get_task.call_args_list]
        assert source_task_id in called_ids, (
            f"应使用 inherit.from='{source_task_id}'，实际调用 get_task 的 ID: {called_ids}"
        )

    @pytest.mark.asyncio
    async def test_workspace_mode_overwrites_old_param_value(self):
        """inherit.mode=workspace 应将 inherit_workspace_from 覆盖为 inherit.from。

        验证：inputs 中 inherit_workspace_from 被设为 inherit.from 的值后，
        后续 workspace 继承逻辑使用的是 inherit.from。
        """
        tool = TaskSubmitTool()
        source_task_id = "task_override_001"
        mock_ws_path = "/tmp/workspace/override_ws"

        mock_old_task = MagicMock()
        mock_old_task.metadata = {"ws_meta": {"path": mock_ws_path}}

        mock_provider = _build_mock_provider(old_task=mock_old_task)

        inputs = _make_minimal_inputs(
            inherit={"from": source_task_id, "mode": "workspace"},
            inherit_workspace_from="old_task_should_be_overridden",
        )

        with (
            patch(_PROVIDER_TARGET, return_value=mock_provider),
            patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
        ):
            result = await tool.execute(inputs)

        assert result.error_code not in ("INVALID_INHERIT_PARAMS", "INVALID_INHERIT_MODE")
        # 确认查找的是 inherit.from 指定的任务
        task_service = mock_provider.get_or_create.return_value
        called_ids = [c.args[0] for c in task_service.get_task.call_args_list]
        assert source_task_id in called_ids
        assert "old_task_should_be_overridden" not in called_ids, (
            "inherit_workspace_from 的旧值不应被使用"
        )


# ══════════════════════════════════════════════════════════
# 五、inherit 参数校验（异常场景）
# ══════════════════════════════════════════════════════════


class TestInheritValidation:
    """验证 inherit 参数的校验逻辑。"""

    @pytest.mark.asyncio
    async def test_inherit_missing_from_returns_error(self):
        """inherit 缺少 from 字段应返回 INVALID_INHERIT_PARAMS。"""
        tool = TaskSubmitTool()
        inputs = _make_minimal_inputs(
            inherit={"mode": "pipe"},
        )
        result = await _run_execute(tool, inputs)
        assert result.error_code == "INVALID_INHERIT_PARAMS"
        assert "from" in result.error

    @pytest.mark.asyncio
    async def test_inherit_missing_mode_returns_error(self):
        """inherit 缺少 mode 字段应返回 INVALID_INHERIT_PARAMS。"""
        tool = TaskSubmitTool()
        inputs = _make_minimal_inputs(
            inherit={"from": "task_001"},
        )
        result = await _run_execute(tool, inputs)
        assert result.error_code == "INVALID_INHERIT_PARAMS"
        assert "mode" in result.error

    @pytest.mark.asyncio
    async def test_inherit_invalid_mode_returns_error(self):
        """inherit.mode 不合法应返回 INVALID_INHERIT_MODE。"""
        tool = TaskSubmitTool()
        inputs = _make_minimal_inputs(
            inherit={"from": "task_001", "mode": "invalid_mode"},
        )
        result = await _run_execute(tool, inputs)
        assert result.error_code == "INVALID_INHERIT_MODE"
        assert "invalid_mode" in result.error

    @pytest.mark.asyncio
    async def test_inherit_empty_from_returns_error(self):
        """inherit.from 为空字符串应返回 INVALID_INHERIT_PARAMS。"""
        tool = TaskSubmitTool()
        inputs = _make_minimal_inputs(
            inherit={"from": "", "mode": "pipe"},
        )
        result = await _run_execute(tool, inputs)
        assert result.error_code == "INVALID_INHERIT_PARAMS"

    @pytest.mark.asyncio
    async def test_inherit_empty_mode_returns_error(self):
        """inherit.mode 为空字符串应返回 INVALID_INHERIT_PARAMS。"""
        tool = TaskSubmitTool()
        inputs = _make_minimal_inputs(
            inherit={"from": "task_001", "mode": ""},
        )
        result = await _run_execute(tool, inputs)
        assert result.error_code == "INVALID_INHERIT_PARAMS"

    @pytest.mark.asyncio
    async def test_inherit_non_dict_ignored(self):
        """inherit 为非 dict 类型时不应触发 inherit 解逻辑（被忽略）。"""
        tool = TaskSubmitTool()
        inputs = _make_minimal_inputs(
            inherit="invalid_string",
        )
        result = await _run_execute(tool, inputs)
        # 非 dict 的 inherit 不进入解析分支，不应返回 inherit 相关错误
        assert result.error_code not in ("INVALID_INHERIT_PARAMS", "INVALID_INHERIT_MODE")


# ══════════════════════════════════════════════════════════
# 六、pipe 继承对话历史传递验证（Bug 修复回归测试）
# ══════════════════════════════════════════════════════════


class TestInheritPipeConversationHistoryBug:
    """回归测试：验证 pipe 继承对话历史的三个 bug 已修复。

    Bug A: task_executor 中 pipe 继承时 user_input="" 导致新任务目标丢失
    Bug B: retry 重建 task_data 时丢失 _inherit_pipe_pipeline_id
    Bug C: _build_metadata 需要持久化 inherit_pipe_from 供 retry 恢复
    """

    @pytest.mark.asyncio
    async def test_pipe_mode_includes_pipeline_id_in_task_data(self):
        """pipe 模式提交时，task_data 应包含 _inherit_pipe_pipeline_id。

        验证：submit_task 收到的 task_data 中有源任务的 pipeline_run_id。
        """
        tool = TaskSubmitTool()
        source_pipeline_id = "pipe_source_6946c0909717"

        # 模拟源任务有 pipeline_run_id
        mock_source_task = MagicMock()
        mock_source_task.pipeline_run_id = source_pipeline_id

        mock_task_service = MagicMock()
        mock_task_service.get_task.return_value = mock_source_task
        mock_task_service.create_task = AsyncMock()
        mock_new_task = MagicMock()
        mock_new_task.id = "new_task_001"
        mock_new_task.title = "新任务"
        mock_new_task.metadata = {}
        mock_task_service.create_task.return_value = mock_new_task
        mock_task_service.hard_delete = AsyncMock()

        mock_task_worker = MagicMock()
        mock_task_worker.submit_task.return_value = True

        mock_agent_config = MagicMock()
        mock_agent_config.level.value = "L2"

        # 工作空间生命周期 mock（task_submit 现在同步调用 on_task_start）
        mock_lifecycle = MagicMock()

        def _on_task_start(task_id, workspace, task_data):  # noqa: ANN001
            mock_new_task.metadata = mock_new_task.metadata or {}
            mock_new_task.metadata["ws_meta"] = {"path": f"/tmp/ws_{task_id}", "mode": "plain"}
            return mock_new_task.metadata["ws_meta"]

        mock_lifecycle.on_task_start.side_effect = _on_task_start

        def provider_get(key):
            if key == "task_worker":
                return mock_task_worker
            if key == "agent_registry":
                reg = MagicMock()
                reg.get.return_value = mock_agent_config
                return reg
            if key == "task_service":
                return mock_task_service
            if key == "workspace_lifecycle_manager":
                return mock_lifecycle
            return None

        mock_provider = MagicMock()
        mock_provider.get_or_create.return_value = mock_task_service
        mock_provider.get.side_effect = provider_get

        inputs = _make_minimal_inputs(
            inherit={"from": "source_task_001", "mode": "pipe"},
        )

        with (
            patch(_PROVIDER_TARGET, return_value=mock_provider),
            patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
        ):
            result = await tool.execute(inputs)

        # 验证 submit_task 被调用且 task_data 包含 _inherit_pipe_pipeline_id
        assert mock_task_worker.submit_task.called, "submit_task 应被调用"
        task_data = mock_task_worker.submit_task.call_args[0][0]
        assert "_inherit_pipe_pipeline_id" in task_data, (
            f"task_data 应包含 _inherit_pipe_pipeline_id，实际 keys: {list(task_data.keys())}"
        )
        assert task_data["_inherit_pipe_pipeline_id"] == source_pipeline_id, (
            f"_inherit_pipe_pipeline_id 应为 {source_pipeline_id}，"
            f"实际为 {task_data.get('_inherit_pipe_pipeline_id')}"
        )

    @pytest.mark.asyncio
    async def test_retry_preserves_inherit_pipe_pipeline_id(self):
        """retry 时应从 metadata.inherit_pipe_from 恢复 _inherit_pipe_pipeline_id。

        验证：_retry_from_terminal 重建的 task_data 包含 _inherit_pipe_pipeline_id。
        """
        from tools.builtin.task.tool import TaskTool
        from tasks.types import TaskModel, TaskStatus
        from tasks.service import TaskService as _TS

        tool = TaskTool()

        source_pipeline_id = "pipe_source_6946c0909717"

        # 构造一个 failed 状态的任务，metadata 中有 inherit_pipe_from
        task = TaskModel(
            id="task_retry_001",
            title="重试任务",
            description="测试重试",
            agent_name="test_agent",
            agent_level="L2",
            status=TaskStatus.FAILED,
            metadata={
                "acceptance_criteria": {"file_check": {"input_params": {"path": "test.txt"}}},
                "max_retries": 6,
                "retry_count": 0,
                "inherit_pipe_from": "source_task_001",
                "target_id": "test_agent",
                "ws_meta": {"path": "/tmp/ws"},
                "isolation_level": "non_isolated",
                "workspace": "/tmp/ws",
            },
            pipeline_run_id="current_pipeline_id",
            parent_pipeline_id="parent_pipeline_id",
        )

        # 模拟源任务有 pipeline_run_id
        mock_source_task = MagicMock()
        mock_source_task.pipeline_run_id = source_pipeline_id

        mock_service = MagicMock(spec=_TS)
        mock_service.get_task.side_effect = lambda tid: (
            task if tid == "task_retry_001" else mock_source_task
        )
        mock_service.force_transition = AsyncMock()
        mock_service.save_task = AsyncMock()

        mock_task_worker = MagicMock()
        mock_task_worker.submit_task.return_value = True

        with (
            patch("tools.builtin.task.tool.TaskTool._get_task_service", return_value=mock_service),
            patch("infrastructure.service_provider.get_service_provider") as mock_sp,
        ):
            mock_sp_inst = MagicMock()
            mock_sp_inst.get.side_effect = lambda key: (
                mock_task_worker if key == "task_worker" else None
            )
            mock_sp.return_value = mock_sp_inst

            result = await tool._retry_from_terminal(task, "", mock_service)

        # 验证 submit_task 被调用
        assert mock_task_worker.submit_task.called, "submit_task 应被调用"
        task_data = mock_task_worker.submit_task.call_args[0][0]

        # 核心断言：retry 重建的 task_data 应包含 _inherit_pipe_pipeline_id
        assert "_inherit_pipe_pipeline_id" in task_data, (
            f"retry 重建的 task_data 应包含 _inherit_pipe_pipeline_id，"
            f"实际 keys: {list(task_data.keys())}"
        )
        assert task_data["_inherit_pipe_pipeline_id"] == source_pipeline_id, (
            f"_inherit_pipe_pipeline_id 应为 {source_pipeline_id}，"
            f"实际为 {task_data.get('_inherit_pipe_pipeline_id')}"
        )

    def test_build_metadata_stores_inherit_pipe_from(self):
        """_build_metadata 应持久化 inherit_pipe_from 供 retry 恢复使用。"""
        tool = TaskSubmitTool()
        inputs = {
            "inherit": {"from": "source_task_001", "mode": "pipe"},
            "session_id": "sess_001",
        }
        goal = {"title": "测试任务"}
        acceptance_criteria = {"file_check": {"input_params": {"path": "test.txt"}}}

        metadata = tool._build_metadata(inputs, goal, acceptance_criteria)

        # inherit_pipe_from 应被持久化
        assert "inherit_pipe_from" in metadata, (
            "metadata 应包含 inherit_pipe_from 字段供 retry 恢复"
        )
        assert metadata["inherit_pipe_from"] == "source_task_001"


def _build_pipe_inherit_provider(
    source_pipeline_id: str,
    mock_task_worker: MagicMock,
    mock_task_service: MagicMock,
    exec_storage=None,
):
    """构造 pipe 继承场景的 service_provider mock。

    exec_storage 为 None 时不提供该服务（模拟 clone 跳过）；
    提供 mock 时 task_submit 会调用其 clone_pipeline_records。
    """
    mock_source_task = MagicMock()
    mock_source_task.pipeline_run_id = source_pipeline_id
    mock_task_service.get_task.return_value = mock_source_task
    mock_task_service.create_task = AsyncMock()
    mock_new_task = MagicMock()
    mock_new_task.id = "new_task_pipe_001"
    mock_new_task.title = "继承任务"
    mock_new_task.metadata = {}
    mock_task_service.create_task.return_value = mock_new_task
    mock_task_service.hard_delete = AsyncMock()

    mock_agent_config = MagicMock()
    mock_agent_config.level.value = "L2"

    mock_lifecycle = MagicMock()

    def _on_task_start(task_id, workspace, task_data):  # noqa: ANN001
        mock_new_task.metadata = mock_new_task.metadata or {}
        mock_new_task.metadata["ws_meta"] = {"path": f"/tmp/ws_{task_id}", "mode": "plain"}
        return mock_new_task.metadata["ws_meta"]

    mock_lifecycle.on_task_start.side_effect = _on_task_start

    def provider_get(key):
        if key == "task_worker":
            return mock_task_worker
        if key == "agent_registry":
            reg = MagicMock()
            reg.get.return_value = mock_agent_config
            return reg
        if key == "task_service":
            return mock_task_service
        if key == "workspace_lifecycle_manager":
            return mock_lifecycle
        if key == "execution_record_storage":
            return exec_storage
        return None

    mock_provider = MagicMock()
    mock_provider.get_or_create.return_value = mock_task_service
    mock_provider.get.side_effect = provider_get
    return mock_provider, mock_new_task


class TestInheritPipeCloneContract:
    """验证 pipe 继承的历史准备（clone）契约。

    task_submit 必须在返回前同步完成 clone：
    - clone 成功 → task_data 带 _pre_pipeline_id，submit_task 被调用
    - clone 失败 → 返回失败给父 LLM，submit_task 不被调用，任务记录被清理
    """

    @pytest.mark.asyncio
    async def test_clone_success_includes_pre_pipeline_id(self):
        """clone 成功时 task_data 应带 _pre_pipeline_id 供 task_executor 复用。"""
        tool = TaskSubmitTool()
        source_pipeline_id = "pipe_source_clone_ok"

        mock_task_worker = MagicMock()
        mock_task_worker.submit_task.return_value = True
        mock_task_service = MagicMock()

        # mock storage：clone 成功，返回记录数
        mock_storage = MagicMock()
        mock_storage.clone_pipeline_records.return_value = 5

        mock_provider, _ = _build_pipe_inherit_provider(
            source_pipeline_id, mock_task_worker, mock_task_service, mock_storage,
        )

        inputs = _make_minimal_inputs(
            inherit={"from": "source_task_001", "mode": "pipe"},
        )

        with (
            patch(_PROVIDER_TARGET, return_value=mock_provider),
            patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
        ):
            result = await tool.execute(inputs)

        assert mock_storage.clone_pipeline_records.called, "clone 应被调用"
        call_kwargs = mock_storage.clone_pipeline_records.call_args.kwargs
        assert call_kwargs["source_pipeline_id"] == source_pipeline_id
        # target_pipeline_id 和 new_container_task_id 应为非空值（预生成 id + 任务 id）
        assert call_kwargs["target_pipeline_id"], "target_pipeline_id 不应为空"
        assert call_kwargs["new_container_task_id"], "new_container_task_id 不应为空"
        # task_data 带 _pre_pipeline_id
        assert mock_task_worker.submit_task.called, "submit_task 应被调用"
        task_data = mock_task_worker.submit_task.call_args[0][0]
        assert "_pre_pipeline_id" in task_data, (
            f"task_data 应含 _pre_pipeline_id，实际 keys: {list(task_data.keys())}"
        )

    @pytest.mark.asyncio
    async def test_clone_failure_returns_error_and_skips_submit(self):
        """clone 失败时 task_submit 应返回失败，且 submit_task 不被调用、任务被清理。"""
        tool = TaskSubmitTool()
        source_pipeline_id = "pipe_source_clone_fail"

        mock_task_worker = MagicMock()
        mock_task_worker.submit_task.return_value = True
        mock_task_service = MagicMock()

        # mock storage：clone 抛异常
        mock_storage = MagicMock()
        mock_storage.clone_pipeline_records.side_effect = ValueError(
            "克隆管道记录失败：源管道无执行记录"
        )

        mock_provider, _ = _build_pipe_inherit_provider(
            source_pipeline_id, mock_task_worker, mock_task_service, mock_storage,
        )

        inputs = _make_minimal_inputs(
            inherit={"from": "source_task_001", "mode": "pipe"},
        )

        with (
            patch(_PROVIDER_TARGET, return_value=mock_provider),
            patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
        ):
            result = await tool.execute(inputs)

        # clone 失败 → submit_task 不应被调用（任务没起来）
        assert not mock_task_worker.submit_task.called, (
            "clone 失败时 submit_task 不应被调用"
        )
        # 任务记录应被清理
        assert mock_task_service.hard_delete.called, (
            "clone 失败时应清理任务记录（hard_delete）"
        )
        # 返回失败结果（ToolExecutionResult 对象）
        assert result.status == "failed", (
            f"clone 失败应返回 failed 状态，实际: {result.status}"
        )
        assert "继承管道历史失败" in (result.error or ""), (
            f"失败原因应含继承失败信息，实际 error: {result.error}"
        )
