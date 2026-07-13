"""POST /api/v1/tasks/root — 手动创建根任务接口测试。

覆盖范围：
1. 非容器根任务：创建 + 自动提交执行（target_id 必填校验、metadata 字段、source 标记）
2. 容器根任务：创建 + 绑定主管道（无需 target_id、容器走现有提交）
3. 校验：非法 task_scope、非容器缺 target_id、workspace 路径安全
4. 复用当前会话主管道（active_pipeline_id）

设计原则：用户手动创建根任务 = L1 主 agent 调 task_submit 的等价行为，
为 L2+ 子 agent 提供合法任务上下文。L2 拦截规则不变。
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from channels.api.deps import APIError, api_error_handler, require_auth
from channels.api.routes_tasks import router

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_auth():
    """覆盖认证依赖，模拟已登录用户。"""

    async def _mock_auth():
        return {"sub": "test_user", "username": "tester"}

    return _mock_auth


def _make_task_model(task_id="t-root-001", title="测试根任务"):
    """构造一个真实 TaskModel 实例（_task_model_to_dict 走 dataclasses.asdict）。"""
    from tasks.types import TaskModel, TaskStatus  # noqa: PLC0415

    return TaskModel(
        id=task_id,
        title=title,
        description="",
        status=TaskStatus.PENDING,
        target_type="agent",
        metadata={
            "task_scope": "non_container",
            "target_id": "agent-x",
            "acceptance_criteria": {},
            "workspace": "",
        },
    )


@pytest.fixture
def client(mock_auth):
    """创建 FastAPI TestClient，覆盖认证与异常处理。下游依赖由 patched_downstream patch。"""
    app = FastAPI()
    app.dependency_overrides[require_auth] = mock_auth
    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _make_container_parent(task_id="c-parent-001", title="父容器"):
    """构造一个容器父任务（task_scope=container），供子任务挂载测试。"""
    from tasks.types import TaskModel, TaskStatus  # noqa: PLC0415

    return TaskModel(
        id=task_id,
        title=title,
        description="",
        status=TaskStatus.RUNNING,
        target_type=None,
        metadata={"task_scope": "container"},
    )


@contextmanager
def patched_downstream(task_model=None, active_pipeline_id="pipe-main-001", session_exists=True, parent_task=None):
    """统一 patch 下游依赖（task_service / 会话 / submit）。

    _submit_task_event 直接 patch 为 AsyncMock，断言其被调用（提交执行契约），
    不深入其内部 task_worker 解析——那是 _submit_task_event 自己的单元测试范围。
    parent_task 非空时，task_service.get_task 返回该父任务（供子任务挂载测试）。
    返回 dict，含 task_service / task_model / submit_mock / session 句柄。
    """
    task_model = task_model or _make_task_model()

    task_service = MagicMock()
    task_service.create_task = AsyncMock(return_value=task_model)
    task_service.bind_pipeline_run = AsyncMock(return_value=None)
    task_service.get_task = MagicMock(return_value=parent_task)
    task_service.list_all = AsyncMock(return_value=[])

    submit_mock = AsyncMock(return_value=True)

    session = MagicMock()
    session.active_pipeline_id = active_pipeline_id

    with patch("channels.api.routes_tasks._get_task_service", return_value=task_service), \
         patch("channels.api.routes_tasks.store") as store_patch, \
         patch("channels.api.routes_tasks._submit_task_event", submit_mock):
        store_patch.get_session.return_value = session if session_exists else None
        yield {
            "task_service": task_service,
            "task_model": task_model,
            "submit_mock": submit_mock,
            "session": session,
        }


# ============================================================
# Test: 非容器根任务
# ============================================================


class TestCreateRootTaskNonContainer:
    """非容器根任务：直接执行。"""

    def test_non_container_success(self, client):
        """非容器根任务创建成功，提交执行，metadata 带 source 标记。"""
        with patched_downstream() as dep:
            resp = client.post("/api/v1/tasks/root", json={
                "title": "跑个测试",
                "description": "细节",
                "task_scope": "non_container",
                "target_id": "agent-x",
                "thread_id": "thread-1",
            })
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["id"] == "t-root-001"
            # task_service.create_task 被调用，且 metadata 含 source/level/session
            dep["task_service"].create_task.assert_awaited_once()
            _kwargs = dep["task_service"].create_task.call_args.kwargs
            assert _kwargs["metadata"]["source"] == "user_manual"
            assert _kwargs["metadata"]["submitted_by_level"] == 1
            assert _kwargs["metadata"]["session_id"] == "thread-1"
            assert _kwargs["metadata"]["task_scope"] == "non_container"
            assert _kwargs["parent_pipeline_id"] == "pipe-main-001"
            assert _kwargs["target_type"] == "agent"
            # 提交执行
            dep["submit_mock"].assert_awaited_once()

    def test_non_container_missing_target_id(self, client):
        """非容器根任务必须指定 target_id。"""
        with patched_downstream():
            resp = client.post("/api/v1/tasks/root", json={
                "title": "缺 agent",
                "task_scope": "non_container",
                "thread_id": "thread-1",
            })

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MISSING_TARGET_AGENT"


# ============================================================
# Test: 容器根任务
# ============================================================


class TestCreateRootTaskContainer:
    """容器根任务：工作空间集合，绑定主管道。"""

    def test_container_success_no_target_id(self, client):
        """容器任务无需 target_id，绑定主管道。"""
        with patched_downstream() as dep:
            resp = client.post("/api/v1/tasks/root", json={
                "title": "工作空间",
                "task_scope": "container",
                "thread_id": "thread-1",
            })

        assert resp.status_code == 201, resp.text
        _kwargs = dep["task_service"].create_task.call_args.kwargs
        assert _kwargs["metadata"]["task_scope"] == "container"
        assert _kwargs["target_type"] is None
        # 容器任务绑定主管道
        dep["task_service"].bind_pipeline_run.assert_awaited_once_with(
            "t-root-001", "pipe-main-001"
        )
        # 也走现有提交逻辑
        dep["submit_mock"].assert_awaited_once()


# ============================================================
# Test: 校验
# ============================================================


class TestCreateRootTaskValidation:
    """入参校验。"""

    def test_invalid_task_scope(self, client):
        """task_scope 只能是 container / non_container。"""
        with patched_downstream():
            resp = client.post("/api/v1/tasks/root", json={
                "title": "x",
                "task_scope": "bogus",
                "thread_id": "thread-1",
            })

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_TASK_SCOPE"

    def test_unsafe_workspace_rejected(self, client):
        """危险工作空间路径被拒。"""
        with patched_downstream():
            resp = client.post("/api/v1/tasks/root", json={
                "title": "x",
                "task_scope": "non_container",
                "target_id": "agent-x",
                "workspace": "/",
                "thread_id": "thread-1",
            })

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "UNSAFE_WORKSPACE"

    def test_task_service_unavailable(self, client):
        """TaskService 不可用时返回 503。"""
        with patch("channels.api.routes_tasks._get_task_service", return_value=None):
            resp = client.post("/api/v1/tasks/root", json={
                "title": "x",
                "thread_id": "thread-1",
            })

        assert resp.status_code == 503


# ============================================================
# Test: 会话主管道复用
# ============================================================


class TestCreateRootTaskSessionPipe:
    """复用当前会话主管道。"""

    def test_no_session_falls_back_empty_pipeline(self, client):
        """会话不存在时 parent_pipeline_id 为 None（不报错）。"""
        with patched_downstream(session_exists=False) as dep:
            resp = client.post("/api/v1/tasks/root", json={
                "title": "x",
                "task_scope": "non_container",
                "target_id": "agent-x",
                "thread_id": "ghost-thread",
            })

        assert resp.status_code == 201
        _kwargs = dep["task_service"].create_task.call_args.kwargs
        assert _kwargs["parent_pipeline_id"] is None


# ============================================================
# Test: 容器子任务（parent_task_id）
# ============================================================


class TestCreateChildTask:
    """容器子任务：挂到容器父任务下，workspace 继承。"""

    def test_child_task_attaches_to_container_parent(self, client):
        """指定 parent_task_id（容器父任务）→ 挂为子任务，is_root=False。"""
        parent = _make_container_parent()
        with patched_downstream(parent_task=parent) as dep:
            resp = client.post("/api/v1/tasks/root", json={
                "title": "子任务",
                "task_scope": "non_container",
                "target_id": "agent-x",
                "thread_id": "thread-1",
                "parent_task_id": "c-parent-001",
            })

        assert resp.status_code == 201, resp.text
        # create_task 收到 parent_task_id
        _kwargs = dep["task_service"].create_task.call_args.kwargs
        assert _kwargs["parent_task_id"] == "c-parent-001"
        # 父任务被查询校验
        dep["task_service"].get_task.assert_called_with("c-parent-001")
        # 提交时 is_root=False（子任务共享父容器 ws）
        dep["submit_mock"].assert_awaited_once()
        assert dep["submit_mock"].call_args.kwargs["is_root"] is False

    def test_child_rejects_non_container_parent(self, client):
        """父任务不是 container → 拒绝。"""
        # parent_task=None 时 get_task 返回 None（不存在）
        with patched_downstream():
            resp = client.post("/api/v1/tasks/root", json={
                "title": "子任务",
                "task_scope": "non_container",
                "target_id": "agent-x",
                "thread_id": "thread-1",
                "parent_task_id": "ghost-parent",
            })

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "PARENT_TASK_NOT_FOUND"


# ============================================================
# Test: 容器任务列表接口
# ============================================================


class TestListContainers:
    """GET /api/v1/tasks/containers — 返回会话的容器任务。"""

    def test_list_containers_filters_container_scope(self, client):
        """只返回 task_scope=container 的任务。"""
        from tasks.types import TaskModel, TaskStatus

        # 混合：一个容器 + 一个非容器
        container = TaskModel(id="c1", title="容器A", status=TaskStatus.RUNNING,
                              metadata={"task_scope": "container"})
        plain = TaskModel(id="t1", title="普通任务", status=TaskStatus.PENDING,
                          metadata={"task_scope": "non_container"})

        with patched_downstream() as dep:
            dep["task_service"].list_all = AsyncMock(return_value=[container, plain])
            resp = client.get("/api/v1/tasks/containers", params={"session_id": "thread-1"})

        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == "c1"
        assert items[0]["title"] == "容器A"
        dep["task_service"].list_all.assert_awaited_once()
        # list_all 收到 session_id
        assert dep["task_service"].list_all.call_args.kwargs.get("session_id") == "thread-1"

