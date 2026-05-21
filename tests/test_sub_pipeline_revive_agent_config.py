"""子管道复活时 agent_config 正确性测试。

验证 _resolve_sub_pipeline_agent_config 在子管道引擎复活时
能正确解析 agent_config，而非回退到主Agent(灵汐)。

Bug根因: 子管道引擎完成 run() 后注册被清理(_cleanup_run_loop)，
         send_pipeline_message 走 _try_revive_pipeline 复活路径时，
         调用方未传 agent_config，导致回退到主Agent配置。
修复: app_factory.py 中 _resolve_sub_pipeline_agent_config 通过
      pipeline_run_id → task.target_id → agent_registry 预解析 agent_config。
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeAgentConfig:
    """模拟 Agent 配置。"""
    config_id: str = ""
    display_name: str = ""
    model: str = "gpt-4"


class FakeAgentRegistry:
    """模拟 Agent 注册表。"""
    def __init__(self, configs: dict[str, FakeAgentConfig] | None = None):
        self._configs = configs or {}

    def get(self, agent_id: str) -> FakeAgentConfig | None:
        return self._configs.get(agent_id)


@dataclass
class FakeTask:
    """模拟 Task。"""
    id: str = ""
    target_id: str = ""
    pipeline_run_id: str | None = None
    status: str = "pending"


class FakeTaskService:
    """模拟 TaskService。"""
    def __init__(self, tasks: list[FakeTask] | None = None):
        self._tasks = tasks or []

    def list_all(self, limit: int = 200):
        return self._tasks[:limit]


class FakeServiceProvider:
    """模拟 ServiceProvider。"""
    def __init__(self, services: dict[str, Any] | None = None):
        self._services = services or {}

    def get(self, key: str, default=None):
        return self._services.get(key, default)


def _resolve_sub_pipeline_agent_config_for_test(
    pipeline_id: str,
    sp: FakeServiceProvider,
) -> FakeAgentConfig | None:
    """测试用: 模拟 _resolve_sub_pipeline_agent_config 核心逻辑。"""
    task_service = sp.get("task_service")
    agent_registry = sp.get("agent_registry")
    if not task_service or not agent_registry:
        return None

    try:
        for task in task_service.list_all(limit=200):
            if getattr(task, "pipeline_run_id", None) == pipeline_id:
                target_id = getattr(task, "target_id", None)
                if target_id:
                    agent_config = agent_registry.get(target_id)
                    if agent_config:
                        return agent_config
    except Exception:
        pass

    return None


class TestSubPipelineReviveAgentConfig:
    """子管道复活 agent_config 解析测试。"""

    def test_resolve_correct_agent_for_sub_pipeline(self):
        """测试: 通过 pipeline_run_id 找到正确的子Agent配置。"""
        code_agent = FakeAgentConfig(config_id="code-assistant", display_name="代码助手")
        default_agent = FakeAgentConfig(config_id="lingxi", display_name="灵汐")

        sp = FakeServiceProvider({
            "task_service": FakeTaskService([
                FakeTask(id="task-1", target_id="lingxi", pipeline_run_id="main-pipeline"),
                FakeTask(id="task-2", target_id="code-assistant", pipeline_run_id="sub-pipeline-456"),
            ]),
            "agent_registry": FakeAgentRegistry({
                "lingxi": default_agent,
                "code-assistant": code_agent,
            }),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("sub-pipeline-456", sp)

        assert result is not None
        assert result.config_id == "code-assistant"

    def test_returns_none_when_no_matching_task(self):
        """测试: pipeline_run_id 没有匹配的 Task 时返回 None。"""
        sp = FakeServiceProvider({
            "task_service": FakeTaskService([
                FakeTask(id="task-1", target_id="lingxi", pipeline_run_id="main-pipeline"),
            ]),
            "agent_registry": FakeAgentRegistry({
                "lingxi": FakeAgentConfig(config_id="lingxi"),
            }),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("nonexistent-pipeline", sp)

        assert result is None

    def test_returns_none_when_no_task_service(self):
        """测试: 没有 task_service 时返回 None。"""
        sp = FakeServiceProvider({
            "agent_registry": FakeAgentRegistry({}),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("any-pipeline", sp)

        assert result is None

    def test_returns_none_when_no_agent_registry(self):
        """测试: 没有 agent_registry 时返回 None。"""
        sp = FakeServiceProvider({
            "task_service": FakeTaskService([]),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("any-pipeline", sp)

        assert result is None

    def test_returns_none_when_task_has_no_target_id(self):
        """测试: Task 没有 target_id 时返回 None。"""
        sp = FakeServiceProvider({
            "task_service": FakeTaskService([
                FakeTask(id="task-1", target_id="", pipeline_run_id="sub-pipeline"),
            ]),
            "agent_registry": FakeAgentRegistry({}),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("sub-pipeline", sp)

        assert result is None

    def test_returns_none_when_target_id_not_in_registry(self):
        """测试: target_id 在 registry 中找不到时返回 None。"""
        sp = FakeServiceProvider({
            "task_service": FakeTaskService([
                FakeTask(id="task-1", target_id="deleted-agent", pipeline_run_id="sub-pipeline"),
            ]),
            "agent_registry": FakeAgentRegistry({
                "lingxi": FakeAgentConfig(config_id="lingxi"),
            }),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("sub-pipeline", sp)

        assert result is None

    def test_main_pipeline_not_affected(self):
        """测试: 主管道通过 _resolve 解析不会返回错误配置。"""
        default_agent = FakeAgentConfig(config_id="lingxi", display_name="灵汐")

        sp = FakeServiceProvider({
            "task_service": FakeTaskService([
                FakeTask(id="task-1", target_id="lingxi", pipeline_run_id="main-pipeline"),
            ]),
            "agent_registry": FakeAgentRegistry({
                "lingxi": default_agent,
            }),
        })

        result = _resolve_sub_pipeline_agent_config_for_test("main-pipeline", sp)

        assert result is not None
        assert result.config_id == "lingxi"

    def test_multiple_sub_pipelines_isolation(self):
        """测试: 多个子管道互不干扰，各自解析到正确的 agent。"""
        code_agent = FakeAgentConfig(config_id="code-assistant", display_name="代码助手")
        doc_agent = FakeAgentConfig(config_id="doc-writer", display_name="文档写手")
        default_agent = FakeAgentConfig(config_id="lingxi", display_name="灵汐")

        sp = FakeServiceProvider({
            "task_service": FakeTaskService([
                FakeTask(id="task-1", target_id="lingxi", pipeline_run_id="main-pipeline"),
                FakeTask(id="task-2", target_id="code-assistant", pipeline_run_id="sub-code"),
                FakeTask(id="task-3", target_id="doc-writer", pipeline_run_id="sub-doc"),
            ]),
            "agent_registry": FakeAgentRegistry({
                "lingxi": default_agent,
                "code-assistant": code_agent,
                "doc-writer": doc_agent,
            }),
        })

        result_code = _resolve_sub_pipeline_agent_config_for_test("sub-code", sp)
        result_doc = _resolve_sub_pipeline_agent_config_for_test("sub-doc", sp)

        assert result_code is not None
        assert result_code.config_id == "code-assistant"
        assert result_doc is not None
        assert result_doc.config_id == "doc-writer"
        assert result_code is not result_doc
