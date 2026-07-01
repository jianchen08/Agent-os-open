"""task_submit 子任务隔离模式继承规则单元测试。

规则（已与需求确认）：
- 父任务是 container（容器任务，metadata.task_scope == "container"）→ 直接子任务
  不继承 isolation_level，使用默认隔离（isolated）模式，清除 LLM 传入值。
- 父任务非 container（含容器孙任务、非容器根任务的子任务）→ 子任务继承直接父任务的
  isolation_level，一律忽略 LLM 显式传入的值（隔离模式为系统控制项）。

测试只覆盖隔离模式继承解析逻辑，源空间（workspace）解析与工作空间初始化均 mock 掉，
不在本测试范围内。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.builtin.task_submit.tool import TaskSubmitTool


# get_service_provider 在 execute 方法内部通过
#   from infrastructure.service_provider import get_service_provider
# 导入，因此 patch 目标必须是 infrastructure.service_provider.get_service_provider
_PROVIDER_TARGET = "infrastructure.service_provider.get_service_provider"


# ── 辅助函数 ──


def _make_minimal_inputs(**overrides) -> dict:
    """构造 execute 所需的最小合法输入，通过 overrides 覆盖/追加字段。"""
    base = {
        "goal": {"title": "测试子任务"},
        "target_type": "agent",
        "target_id": "test_agent",
        "acceptance_criteria": {"file_check": {"input_params": {"path": "test.txt"}}},
        "parent_agent_level": 1,
    }
    base.update(overrides)
    return base


def _build_mock_provider(
    *,
    parent_task: MagicMock,
    new_task: MagicMock | None = None,
) -> MagicMock:
    """构建 mock provider，get_task 按 task_id 区分父任务与新建任务。"""
    if new_task is None:
        new_task = MagicMock()
        new_task.id = "task_new_001"
        new_task.title = "测试子任务"
        new_task.status.value = "pending"
        new_task.metadata = {}

    mock_task_service = MagicMock()
    mock_task_service.create_task = AsyncMock(return_value=new_task)
    # get_task(parent_task_id) → 父任务；其它查询 → 新建任务
    mock_task_service.get_task.side_effect = lambda tid: (
        parent_task if tid == parent_task.id else new_task
    )
    mock_task_service.hard_delete = AsyncMock()
    mock_task_service.get_root_task_id.return_value = ""

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
        if key == "execution_record_storage":
            return None
        return None

    mock_provider = MagicMock()
    mock_provider.get_or_create.return_value = mock_task_service
    mock_provider.get.side_effect = provider_get

    return mock_provider


def _make_parent_task(
    *,
    parent_id: str = "task_parent_001",
    task_scope: str = "non_container",
    isolation_level: str | None = None,
) -> MagicMock:
    """构造父任务 mock。"""
    task = MagicMock()
    task.id = parent_id
    metadata: dict = {"task_scope": task_scope}
    if isolation_level:
        metadata["isolation_level"] = isolation_level
    task.metadata = metadata
    return task


async def _run_execute(
    tool: TaskSubmitTool,
    inputs: dict,
    parent_task: MagicMock,
) -> tuple[Any, MagicMock]:
    """执行 task_submit（带全套 mock），返回 (结果, mock_task_service)。"""
    mock_provider = _build_mock_provider(parent_task=parent_task)
    with (
        patch(_PROVIDER_TARGET, return_value=mock_provider),
        patch("tools.builtin.task_submit.tool.os.path.exists", return_value=True),
    ):
        result = await tool.execute(inputs)
    mock_task_service = mock_provider.get("task_service")
    return result, mock_task_service


# ══════════════════════════════════════════════════════════
# 一、容器直接子任务：不继承
# ══════════════════════════════════════════════════════════


class TestContainerChildNoInherit:
    """父任务是 container 时，直接子任务不应继承 isolation_level。"""

    @pytest.mark.asyncio
    async def test_container_child_strips_llm_host(self):
        """父任务 container（isolation_level=non_isolated），子任务 LLM 传 non_isolated → 子任务 metadata 无 isolation_level。"""
        tool = TaskSubmitTool()
        parent_task = _make_parent_task(
            task_scope="container", isolation_level="non_isolated",
        )
        inputs = _make_minimal_inputs(
            parent_task_id=parent_task.id,
            isolation_level="non_isolated",
        )

        result, mock_task_service = await _run_execute(tool, inputs, parent_task)

        assert result.error_code != "TASK_CREATE_FAILED", f"任务创建失败: {result.error}"
        metadata = mock_task_service.create_task.call_args.kwargs.get("metadata", {})
        assert "isolation_level" not in metadata, (
            f"容器直接子任务不应携带 isolation_level，实际 metadata: {metadata}"
        )

    @pytest.mark.asyncio
    async def test_container_child_default_when_parent_host(self):
        """父任务 container + isolation_level=non_isolated，子任务未传 → 子任务 metadata 无 isolation_level。"""
        tool = TaskSubmitTool()
        parent_task = _make_parent_task(
            task_scope="container", isolation_level="non_isolated",
        )
        inputs = _make_minimal_inputs(parent_task_id=parent_task.id)

        result, mock_task_service = await _run_execute(tool, inputs, parent_task)

        metadata = mock_task_service.create_task.call_args.kwargs.get("metadata", {})
        assert "isolation_level" not in metadata


# ══════════════════════════════════════════════════════════
# 二、非容器父任务的子任务：继承直接父任务 isolation_level
# ══════════════════════════════════════════════════════════


class TestNonContainerChildInherits:
    """父任务非 container 时，子任务应继承直接父任务的 isolation_level。"""

    @pytest.mark.asyncio
    async def test_grandchild_inherits_host(self):
        """父任务非 container + isolation_level=non_isolated → 子任务 metadata isolation_level=non_isolated。"""
        tool = TaskSubmitTool()
        parent_task = _make_parent_task(
            task_scope="non_container", isolation_level="non_isolated",
        )
        inputs = _make_minimal_inputs(parent_task_id=parent_task.id)

        result, mock_task_service = await _run_execute(tool, inputs, parent_task)

        metadata = mock_task_service.create_task.call_args.kwargs.get("metadata", {})
        assert metadata.get("isolation_level") == "non_isolated", (
            f"孙任务应继承父任务 isolation_level=non_isolated，实际 metadata: {metadata}"
        )

    @pytest.mark.asyncio
    async def test_grandchild_inherits_container(self):
        """父任务非 container + isolation_level=isolated → 子任务 metadata isolation_level=isolated。"""
        tool = TaskSubmitTool()
        parent_task = _make_parent_task(
            task_scope="non_container", isolation_level="isolated",
        )
        inputs = _make_minimal_inputs(parent_task_id=parent_task.id)

        result, mock_task_service = await _run_execute(tool, inputs, parent_task)

        metadata = mock_task_service.create_task.call_args.kwargs.get("metadata", {})
        assert metadata.get("isolation_level") == "isolated"

    @pytest.mark.asyncio
    async def test_grandchild_ignores_llm_value(self):
        """父任务 isolation_level=isolated，LLM 传 non_isolated → 子任务继承 isolated，丢弃 non_isolated。"""
        tool = TaskSubmitTool()
        parent_task = _make_parent_task(
            task_scope="non_container", isolation_level="isolated",
        )
        inputs = _make_minimal_inputs(
            parent_task_id=parent_task.id,
            isolation_level="non_isolated",
        )

        result, mock_task_service = await _run_execute(tool, inputs, parent_task)

        metadata = mock_task_service.create_task.call_args.kwargs.get("metadata", {})
        assert metadata.get("isolation_level") == "isolated", (
            f"孙任务应忽略 LLM 的 non_isolated 改为继承父任务 isolated，实际 metadata: {metadata}"
        )

    @pytest.mark.asyncio
    async def test_child_no_isolation_when_parent_none(self):
        """父任务非 container 且无 isolation_level → 子任务 metadata 无 isolation_level（走默认），LLM 值也被忽略。"""
        tool = TaskSubmitTool()
        parent_task = _make_parent_task(
            task_scope="non_container", isolation_level=None,
        )
        inputs = _make_minimal_inputs(
            parent_task_id=parent_task.id,
            isolation_level="non_isolated",
        )

        result, mock_task_service = await _run_execute(tool, inputs, parent_task)

        metadata = mock_task_service.create_task.call_args.kwargs.get("metadata", {})
        assert "isolation_level" not in metadata, (
            f"父任务无 isolation_level 时子任务应走默认，实际 metadata: {metadata}"
        )
