# @feature: channel_api 拆迁批次1/3 | @ci: python-coverage
"""tasks 插件自持 HTTP 面测试（http_api.handle_http 分发 + projects 接真生命周期）。

覆盖（对照 docs/working/channel_api插件拆迁方案_20260821.md 批次1 tasks 域 +
批次3 projects 域）：

1. manifest：plugin.json http_endpoints 28 条声明（命名空间/枚举/auth/timeout）。
2. 分发：handle_http 全部路径→handler 接线（28 端点形状走通，含 404/400 语义）。
3. tasks 域：创建（agent_id 必填校验）/列表（状态+分页）/详情（跨用户 404）/
   删除/提交（状态闸）/评估（引擎降级）/暂停/恢复/取消（管道 fake + 级联）/
   根任务（scope 校验 + 父容器校验）/容器列表/update 读 state 聚合。
4. phase/ac 域：状态→阶段映射 + 占位语义。
5. projects 域接真：创建项目 = 容器任务（task_scope=container + workspace 关联
   元数据 ws_meta + 副作用日志），list/get/pause/resume/auto-execute/delete
   容器任务生命周期；响应形状对齐 frontend/src/types/task.ts Project。

[来源: docs/working/channel_api插件拆迁方案_20260821.md]
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出同名裸模块，强制按本插件目录解析（与 test_tasks_plugin.py 同款）。

    http_api 模块级 import pydantic/服务模块；server 仅在懒路径被触达，
    仍一并逐出防跨插件污染。
    """
    d = str(_PLUGIN_DIR)
    _was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    for m in (
        "task_types",
        "state_machine",
        "storage",
        "service",
        "timer_manager",
        "agents_types",
        "enum_utils",
        "workspace",
        "service_access",
        "_task_cleanup",
        "_task_crud",
        "_task_state",
        "http_api",
        "server",
    ):
        sys.modules.pop(m, None)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if _was_present:
        sys.path.insert(0, d)


# ═══════════════════════════════════════════════════════════
# 测试脚手架
# ═══════════════════════════════════════════════════════════


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应（未预置 → KeyError 语义）。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method not in self._responses:
            raise KeyError(f"unexpected capability method: {method}")
        return self._responses[method]


class _FakeCapabilityHub:
    """fake http_api._capability 入口：按能力名返回 fake 句柄。"""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.handles: dict[str, _FakeCapability] = {}

    def get(self, name: str) -> _FakeCapability:
        if name not in self.handles:
            self.handles[name] = self._FakeCapability(name, self._responses.get(name, {}))
        return self.handles[name]

    class _FakeCapability(_FakeCapability):
        def __init__(self, name: str, responses: dict[str, Any]) -> None:
            super().__init__(responses)
            self.name = name


@pytest.fixture
def service():
    """临时目录 TaskService 实例（data_dir 注入，避免触碰真实数据目录）。"""
    from service import TaskService

    svc = TaskService(data_dir=tempfile.mkdtemp(prefix="test_tasks_http_"))
    return svc


@pytest.fixture
def hub() -> _FakeCapabilityHub:
    return _FakeCapabilityHub({})


def _install(monkeypatch: pytest.MonkeyPatch, svc: Any, hub: _FakeCapabilityHub) -> None:
    """注入服务与能力句柄（http_api 内部引用点）。"""
    import http_api

    monkeypatch.setattr(http_api, "_get_task_service", lambda: svc)
    monkeypatch.setattr(http_api, "_capability", hub.get)


async def _http(monkeypatch: pytest.MonkeyPatch, svc: Any, hub: _FakeCapabilityHub,
                path: str, method: str = "GET", body: dict[str, Any] | None = None,
                query: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """走完整 handle_http 分发，返回解码后的 HTTP 响应（{status, payload}）。"""
    import http_api

    _install(monkeypatch, svc, hub)
    raw_body = json.dumps(body) if body is not None else ""
    result = await http_api.handle_http(path, method, raw_body, query, headers)
    assert result.get("success") is True, f"handle_http 失败: {result}"
    data = result["data"]
    payload = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return {"status": data["status"], "payload": payload}


def _make_token(user_id: str, username: str = "tester") -> str:
    """构造内核 0.2 开发期 token（base64_nopad("access:{uid}:{name}:{exp}")）。"""
    raw = f"access:{user_id}:{username}:{2**31 - 1}"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


async def _seed_container(svc: Any, title: str = "容器项目", **meta: Any) -> Any:
    """种一个容器任务（task_scope=container）。"""
    metadata = {"task_scope": "container", "session_id": "sess-1", "user_id": "u-1", **meta}
    return await svc.create_task(title=title, metadata=metadata)


# ═══════════════════════════════════════════════════════════
# 1. manifest 声明
# ═══════════════════════════════════════════════════════════

class TestManifestHttpEndpoints:
    """plugin.json http_endpoints 声明与分发语义一致。"""

    def test_declares_28_endpoints(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        eps = manifest["http_endpoints"]
        assert len(eps) == 28, f"channel_api tasks(21)+projects(7)=28，实际 {len(eps)}"

    def test_namespace_auth_timeout(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        for e in manifest["http_endpoints"]:
            assert e["path"].startswith("/ext/task_service/"), e["path"]
            assert e["auth"] == "user", e["path"]
            assert e["handler_capability"] == "http.handle", e["path"]
            assert e["timeout_ms"] == 5000, e["path"]

    def test_routes_cover_tasks_and_projects_domains(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        paths = {(e["method"], e["path"]) for e in manifest["http_endpoints"]}
        assert ("GET", "/ext/task_service/tasks") in paths
        assert ("POST", "/ext/task_service/tasks/root") in paths
        assert ("GET", "/ext/task_service/tasks/containers") in paths
        assert ("PATCH", "/ext/task_service/tasks/{task_id}") in paths
        assert ("DELETE", "/ext/task_service/tasks/{task_id}") in paths
        assert ("POST", "/ext/task_service/tasks/{task_id}/cancel") in paths
        assert ("GET", "/ext/task_service/tasks/{task_id}/phase") in paths
        assert ("GET", "/ext/task_service/tasks/{task_id}/ac") in paths
        assert ("POST", "/ext/task_service/projects") in paths
        assert ("POST", "/ext/task_service/projects/{project_id}/auto-execute") in paths
        assert ("DELETE", "/ext/task_service/projects/{project_id}") in paths


# ═══════════════════════════════════════════════════════════
# 2. 分发层（404 / 未匹配）
# ═══════════════════════════════════════════════════════════

class TestDispatch:
    async def test_unknown_path_404(self, monkeypatch: pytest.MonkeyPatch, service: Any,
                                    hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/nope")
        assert resp["status"] == 404

    async def test_outside_namespace_404(self, monkeypatch: pytest.MonkeyPatch, service: Any,
                                         hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/other/tasks")
        assert resp["status"] == 404

    async def test_invalid_json_body_400(self, monkeypatch: pytest.MonkeyPatch, service: Any,
                                         hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, service, hub)
        result = await http_api.handle_http(
            "/ext/task_service/tasks", "POST", "not-json-at-all", {}, None,
        )
        assert result["data"]["status"] == 400


# ═══════════════════════════════════════════════════════════
# 3. tasks 域端点
# ═══════════════════════════════════════════════════════════

class TestTasksEndpoints:
    async def test_create_task_requires_agent(self, monkeypatch: pytest.MonkeyPatch,
                                              service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "x"})
        assert resp["status"] == 400
        # 契约（channel_api _http_exc_response 原样）：detail = 错误消息文本
        assert "必须指定执行 Agent" in resp["payload"]["detail"]

    async def test_create_task_via_chat(self, monkeypatch: pytest.MonkeyPatch,
                                        service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-created-1"}}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "测试任务", "agent_id": "main"})
        assert resp["status"] == 200
        t = resp["payload"]
        assert t["id"] == "p-created-1"
        assert t["title"] == "测试任务"
        assert t["status"] == "pending"

    async def test_list_tasks_status_filter(self, monkeypatch: pytest.MonkeyPatch,
                                            service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="A")
        t2 = await service.create_task(title="B")
        await service.start_task(t2.id)
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"status": "running"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["title"] == "B"

    async def test_list_tasks_session_filter(self, monkeypatch: pytest.MonkeyPatch,
                                             service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="S", metadata={"session_id": "sess-a"})
        await service.create_task(title="O")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"session_id": "sess-a"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["title"] == "S"

    async def test_get_task_ok_and_404(self, monkeypatch: pytest.MonkeyPatch,
                                       service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="详情")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}")
        assert resp["status"] == 200
        assert resp["payload"]["title"] == "详情"
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/missing-1")
        assert resp["status"] == 404

    async def test_get_task_cross_user_404(self, monkeypatch: pytest.MonkeyPatch,
                                           service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="私有", metadata={"user_id": "u-owner"})
        headers = {"Authorization": f"Bearer {_make_token('u-other')}"}
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}",
                           headers=headers)
        assert resp["status"] == 404

    async def test_delete_task(self, monkeypatch: pytest.MonkeyPatch,
                               service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="删")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}", "DELETE")
        assert resp["status"] == 200
        assert resp["payload"]["message"] == "任务已删除"
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/missing-1", "DELETE")
        assert resp["status"] == 404

    async def test_submit_task_state_gate(self, monkeypatch: pytest.MonkeyPatch,
                                          service: Any, hub: _FakeCapabilityHub) -> None:
        # pending 可提交（注入模式），running 拒绝
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-retry"}}
        t = await service.create_task(title="提交")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}/submit",
                           "POST")
        assert resp["status"] == 200
        assert resp["payload"]["task_id"] == t.id

        t2 = await service.create_task(title="运行中")
        await service.start_task(t2.id)
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t2.id}/submit",
                           "POST")
        assert resp["status"] == 400

    async def test_evaluate_task_engine_degraded(self, monkeypatch: pytest.MonkeyPatch,
                                                 service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="评")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}/evaluate",
                           "POST", body={})
        assert resp["status"] == 200
        assert resp["payload"]["summary"] == "评估引擎不可用"
        assert resp["payload"]["results"] == []

    async def test_pause_resume_task(self, monkeypatch: pytest.MonkeyPatch,
                                     service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "r-1"},
            "resume_pipeline": {"run_id": "r-2"},
        }
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/pause", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["success"] is True
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/resume", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["resumed_count"] == 1

    async def test_pause_task_no_run_404(self, monkeypatch: pytest.MonkeyPatch,
                                         service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {}}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/pause", "POST")
        assert resp["status"] == 404

    async def test_cancel_task_cascade(self, monkeypatch: pytest.MonkeyPatch,
                                       service: Any, hub: _FakeCapabilityHub) -> None:
        parent = await service.create_task(title="父")
        child = await service.create_task(title="子")
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        hub._responses["pipeline-state"] = {
            "list": [
                {"pipeline_id": child.id, "lineage.parent_pipeline_id": parent.id},
            ],
        }
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/tasks/{parent.id}/cancel", "POST")
        assert resp["status"] == 200
        # 源行为原样：任务存在 → 返回取消后的任务 dict（fallback 消息形状仅任务缺失时出现）
        assert resp["payload"]["id"] == parent.id
        # 级联挂起：父管道 + lineage 子管道都在 suspend_pipeline 调用里
        suspend_ids = [p["pipeline_id"] for m, p in hub.handles["pipeline-executor"].calls
                       if m == "suspend_pipeline"]
        assert parent.id in suspend_ids and child.id in suspend_ids

    async def test_create_root_task_validation(self, monkeypatch: pytest.MonkeyPatch,
                                               service: Any, hub: _FakeCapabilityHub) -> None:
        # 非容器缺 target_id → 400；非法 scope → 400；父容器不存在 → 400
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根", "thread_id": "th-1",
                                 "task_scope": "non_container", "target_id": ""})
        assert resp["status"] == 400
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根", "thread_id": "th-1", "task_scope": "bogus"})
        assert resp["status"] == 400
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根", "thread_id": "th-1", "task_scope": "container",
                                 "parent_task_id": "no-such"})
        assert resp["status"] == 400
        assert "父任务不存在" in resp["payload"]["detail"]

    async def test_create_root_task_container_ok(self, monkeypatch: pytest.MonkeyPatch,
                                                 service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-root-1"}}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "容器根", "thread_id": "th-1",
                                 "task_scope": "container"})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-root-1"

    async def test_list_container_tasks(self, monkeypatch: pytest.MonkeyPatch,
                                        service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="容器A")
        await service.create_task(title="普通")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/containers",
                           query={"session_id": "sess-1"})
        assert resp["status"] == 200
        assert resp["payload"] == [{"id": c.id, "title": "容器A"}]

    async def test_update_task_reads_state_rows(self, monkeypatch: pytest.MonkeyPatch,
                                                service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-state"] = {
            "list": [{"pipeline_id": "p-9", "task.goal": "目标九", "task.status": "running"}],
        }
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/p-9", "PATCH",
                           body={"title": "ignored"})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-9"
        assert resp["payload"]["title"] == "目标九"
        assert resp["payload"]["status"] == "running"

    async def test_get_tasks_debug(self, monkeypatch: pytest.MonkeyPatch,
                                   service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="调试A", metadata={"session_id": "s"})
        await service.create_task(title="调试B", metadata={"session_id": "s"})
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/debug/all",
                           query={"limit": "10"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 2


# ═══════════════════════════════════════════════════════════
# 4. task_phase / ac 域端点
# ═══════════════════════════════════════════════════════════

class TestPhaseAndAceEndpoints:
    async def test_get_task_phase_mapping(self, monkeypatch: pytest.MonkeyPatch,
                                          service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="阶段")
        await service.start_task(t.id)  # running
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}/phase")
        assert resp["payload"]["currentPhase"] == "execute"
        assert resp["payload"]["phaseStatus"] == "running"

    async def test_get_task_phase_missing_task_default(self, monkeypatch: pytest.MonkeyPatch,
                                                       service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/missing/phase")
        assert resp["payload"]["currentPhase"] == "prepare"
        assert resp["payload"]["phaseStatus"] == "pending"

    async def test_complete_phases(self, monkeypatch: pytest.MonkeyPatch,
                                   service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/t-1/phase/prepare/complete", "POST")
        assert resp["payload"]["current_phase"] == "execute"
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/t-1/phase/execute/complete", "POST")
        assert resp["payload"]["current_phase"] == "review"

    async def test_phase_output(self, monkeypatch: pytest.MonkeyPatch,
                                service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/t-1/phase/execute/output")
        assert resp["payload"] == {"output": None, "error": None}

    async def test_ac_endpoints_shapes(self, monkeypatch: pytest.MonkeyPatch,
                                       service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/ac")
        assert resp["payload"] == {"taskId": "t-1", "acceptanceCriteria": []}
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/t-1/ac/evaluate-all", "POST")
        assert resp["payload"] == {"taskId": "t-1", "acceptanceCriteria": []}
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/t-1/ac/ac-1/evaluate", "POST")
        assert resp["payload"]["acceptance_criterion"]["status"] == "not_evaluated"
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/ac/ac-1/result")
        assert resp["payload"]["acceptance_criterion"]["passed"] is None

    async def test_phase_unknown_subroute_404(self, monkeypatch: pytest.MonkeyPatch,
                                              service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/t-1/phase/unknown/action", "POST")
        assert resp["status"] == 404


# ═══════════════════════════════════════════════════════════
# 5. projects 域（接真 = 容器任务生命周期）
# ═══════════════════════════════════════════════════════════

class TestProjectsLifecycle:
    async def test_create_project_creates_container_task(
            self, monkeypatch: pytest.MonkeyPatch, service: Any, hub: _FakeCapabilityHub) -> None:
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "长期目标", "session_id": "sess-1",
                                 "auto_execute": True},
                           headers=headers)
        assert resp["status"] == 200
        proj = resp["payload"]["project"]
        assert proj["goal"] == "长期目标"
        assert proj["userId"] == "u-1"
        assert proj["sessionId"] == "sess-1"
        assert proj["autoExecute"] is True
        assert proj["status"] == "running"  # 容器任务创建即自动启动
        assert proj["timestamps"]["createdAt"]
        # 容器任务本体可查（project_id = container_task_id）
        task = service.get_task(proj["id"])
        assert task is not None
        assert (task.metadata or {}).get("task_scope") == "container"
        # workspace 关联元数据可观测
        ws_meta = (task.metadata or {}).get("ws_meta", {})
        assert ws_meta.get("mode") == "worktree"

    async def test_create_project_requires_goal(self, monkeypatch: pytest.MonkeyPatch,
                                                service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects", "POST", body={})
        assert resp["status"] == 400
        assert "必须指定 goal" in resp["payload"]["detail"]

    async def test_list_projects(self, monkeypatch: pytest.MonkeyPatch,
                                 service: Any, hub: _FakeCapabilityHub) -> None:
        await _seed_container(service, title="项目A", user_id="u-1")
        await _seed_container(service, title="项目B", user_id="u-1", auto_execute=True)
        await service.create_task(title="非项目")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects",
                           query={"limit": "20"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 2
        assert resp["payload"]["limit"] == 20
        items = resp["payload"]["items"]
        assert {p["goal"] for p in items} == {"项目A", "项目B"}

    async def test_list_projects_status_filter(self, monkeypatch: pytest.MonkeyPatch,
                                               service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="暂停的")
        await service.pause_task(c.id)  # running → stopped
        await _seed_container(service, title="运行中")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects",
                           query={"status": "suspended"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["goal"] == "暂停的"

    async def test_get_project_with_subtasks(self, monkeypatch: pytest.MonkeyPatch,
                                             service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="项目C")
        child = await service.create_task(title="子任务", parent_task_id=c.id)
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/projects/{c.id}")
        assert resp["status"] == 200
        proj = resp["payload"]["project"]
        assert proj["id"] == c.id
        assert [t["id"] for t in proj["tasks"]] == [child.id]

    async def test_get_project_rejects_non_container(self, monkeypatch: pytest.MonkeyPatch,
                                                     service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="普通任务")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/projects/{t.id}")
        assert resp["status"] == 404

    async def test_pause_resume_project(self, monkeypatch: pytest.MonkeyPatch,
                                        service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "r-1"},
            "resume_pipeline": {"run_id": "r-2"},
        }
        c = await _seed_container(service, title="项目D")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/projects/{c.id}/pause",
                           "POST")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["status"] == "suspended"
        # 容器任务记录同步落 STOPPED
        assert str(service.get_task(c.id).status.value) == "stopped"

        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/projects/{c.id}/resume",
                           "POST")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["status"] == "running"
        assert str(service.get_task(c.id).status.value) == "running"

    async def test_pause_project_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                             service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects/no-such/pause",
                           "POST")
        assert resp["status"] == 404

    async def test_toggle_auto_execute(self, monkeypatch: pytest.MonkeyPatch,
                                       service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="项目E")  # auto_execute 默认 False
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/projects/{c.id}/auto-execute", "POST",
                           body={"enabled": True})
        assert resp["status"] == 200
        assert resp["payload"]["project"]["autoExecute"] is True
        assert (service.get_task(c.id).metadata or {}).get("auto_execute") is True
        # 缺省 enabled → 翻转现值
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/projects/{c.id}/auto-execute", "POST", body={})
        assert resp["payload"]["project"]["autoExecute"] is False

    async def test_delete_project_soft_delete(self, monkeypatch: pytest.MonkeyPatch,
                                              service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="项目F")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/projects/{c.id}",
                           "DELETE")
        assert resp["status"] == 200
        assert resp["payload"]["id"] == c.id
        # 容器任务软删除：记录保留 + soft_deleted 标记，列表不再出现
        task = service.get_task(c.id)
        assert task is not None
        assert (task.metadata or {}).get("soft_deleted") is True
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects")
        assert resp["payload"]["total"] == 0
        # 软删除幂等（与 tasks 域 delete_task 容器语义一致）：重删仍 200，记录保留
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/projects/{c.id}", "DELETE")
        assert resp["status"] == 200
        assert resp["payload"]["id"] == c.id

    async def test_project_created_via_dispatch_is_container(
            self, monkeypatch: pytest.MonkeyPatch, service: Any, hub: _FakeCapabilityHub) -> None:
        """端到端：dispatch 创建 → /containers 可见（容器任务体系打通）。"""
        await _http(monkeypatch, service, hub, "/ext/task_service/projects", "POST",
                    body={"goal": "端到端项目", "session_id": "sess-9"})
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/containers",
                           query={"session_id": "sess-9"})
        assert len(resp["payload"]) == 1
        assert resp["payload"][0]["title"] == "端到端项目"