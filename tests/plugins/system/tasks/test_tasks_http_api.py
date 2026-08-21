# @feature: channel_api 拆迁批次1/3 | @ci: python-coverage
"""tasks 插件自持 HTTP 面测试（http_api.handle_http 分发 + projects 接真生命周期）。

测试置于 tests/plugins/system/（插桩车道）：tasks 插件整体在 plugins-heavy
免插桩豁免名单内（scripts/coverage_exempt.py EXEMPT_SUITES），插件目录内
test_*.py 不产生 coverage.xml 度量；本套件迁入此目录后 http_api.py 才进入
改动行覆盖率门禁（check_diff_coverage.py）可度量面，与 workspace/artifacts
拆迁批次测试同构。

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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "tasks"


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


# ═══════════════════════════════════════════════════════════
# 6. 边界与降级路径（http_api.py 剩余可及分支）
# ═══════════════════════════════════════════════════════════


class _CrashService:
    """存储层故障 stub：所有方法抛 RuntimeError（模拟 YAML 存储损坏/IO 失败）。"""

    async def list_all(self, limit: int = 1000, session_id: str | None = None,
                       reverse: bool = False) -> Any:
        raise RuntimeError("storage boom")

    async def create_task(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("storage boom")

    async def delete_task(self, task_id: str) -> bool:
        raise RuntimeError("storage boom")

    def get_task(self, task_id: str) -> Any | None:
        return None


class TestEdgeAndDegradedBranches:
    # ── 分页/列表筛选边界 ──

    async def test_list_tasks_pagination_validation(self, monkeypatch: pytest.MonkeyPatch,
                                                    service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"limit": "0"})
        assert resp["status"] == 400
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"limit": "500"})
        assert resp["status"] == 400
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"offset": "-1"})
        assert resp["status"] == 400

    async def test_list_tasks_priority_filter(self, monkeypatch: pytest.MonkeyPatch,
                                              service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="低", priority=9)
        await service.create_task(title="高", priority=1)
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"priority": "9"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["title"] == "低"

    async def test_list_tasks_agent_level_from_metadata(self, monkeypatch: pytest.MonkeyPatch,
                                                        service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="带级别", agent_level=None,
                                  metadata={"agent_level": "L2"})
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks")
        item = next(i for i in resp["payload"]["items"] if i["title"] == "带级别")
        assert item["agent_level"] == "L2"

    async def test_list_tasks_service_failure_empty(self, monkeypatch: pytest.MonkeyPatch,
                                                    hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, _CrashService(), hub, "/ext/task_service/tasks")
        assert resp["status"] == 200
        assert resp["payload"] == {"items": [], "total": 0}

    async def test_get_tasks_debug_filters(self, monkeypatch: pytest.MonkeyPatch,
                                           service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="d1", metadata={"session_id": "s1"})
        t2 = await service.create_task(title="d2", metadata={"session_id": "s2"})
        await service.start_task(t2.id)
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/debug/all",
                           query={"status": "running"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["title"] == "d2"
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/debug/all",
                           query={"session_id": "s1"})
        assert resp["payload"]["total"] == 1

    async def test_get_tasks_debug_service_failure_empty(self, monkeypatch: pytest.MonkeyPatch,
                                                         hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, _CrashService(), hub,
                           "/ext/task_service/tasks/debug/all")
        assert resp["status"] == 200
        assert resp["payload"] == {"items": [], "total": 0}

    async def test_container_list_service_failure_empty(self, monkeypatch: pytest.MonkeyPatch,
                                                        hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, _CrashService(), hub,
                           "/ext/task_service/tasks/containers")
        assert resp["status"] == 200
        assert resp["payload"] == []

    # ── 响应协议/身份解析辅助 ──

    def test_http_exc_response_uses_detail_attr(self) -> None:
        import http_api

        class _Err(Exception):
            status_code = 418
            detail = "teapot-required"

        out = http_api._http_exc_response(_Err())
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload == {"detail": "teapot-required"}
        assert out["data"]["status"] == 418

    def test_decode_kernel_token_malformed(self) -> None:
        import http_api

        assert http_api._decode_kernel_token("!!!not-base64!!!") is None
        short = base64.b64encode(b"access:user:n").decode("ascii")
        assert http_api._decode_kernel_token(short) is None
        badexp = base64.b64encode(b"access:user:n:notnum").decode("ascii")
        assert http_api._decode_kernel_token(badexp) is None

    def test_resolve_caller_edge(self) -> None:
        import http_api

        assert http_api._resolve_caller(None) == {}
        assert http_api._resolve_caller({"X-Header": "1"}) == {}
        expired = base64.b64encode(b"access:u:name:1").decode("ascii").rstrip("=")
        assert http_api._resolve_caller({"Authorization": f"Bearer {expired}"}) == {}

    def test_capability_not_injected_raises_keyerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import http_api
        import types

        fake = types.ModuleType("server")

        class _Plugin:
            def get_capability(self, name: str) -> Any:
                raise LookupError(name)

        fake.plugin = _Plugin()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "server", fake)
        with pytest.raises(KeyError):
            http_api._capability("pipeline-state")

    # ── 能力缺失/下游故障降级 ──

    async def test_pause_resume_capability_failure_404(self, monkeypatch: pytest.MonkeyPatch,
                                                       service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {}  # 方法缺失 → fake call KeyError
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/pause", "POST")
        assert resp["status"] == 404
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/resume", "POST")
        assert resp["status"] == 404

    async def test_cancel_cascade_rows_not_list(self, monkeypatch: pytest.MonkeyPatch,
                                                service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        hub._responses["pipeline-state"] = {"list": {"not": "a list"}}
        t = await service.create_task(title="级联降级")
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/tasks/{t.id}/cancel", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["id"] == t.id

    async def test_cancel_cascade_capability_missing(self, monkeypatch: pytest.MonkeyPatch,
                                                     service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        # 无 pipeline-state 响应 → 级联异常 → 0，不阻断响应
        t = await service.create_task(title="级联缺失")
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/tasks/{t.id}/cancel", "POST")
        assert resp["status"] == 200

    async def test_cancel_task_missing_fallback_shape(self, monkeypatch: pytest.MonkeyPatch,
                                                      service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/no-such/cancel", "POST")
        assert resp["status"] == 200
        payload = resp["payload"]
        assert payload["cancelled"] is True
        assert payload["message"] == "任务已取消"
        assert payload["cascaded_subtasks"] == 0

    async def test_submit_task_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                           service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/no-such/submit", "POST")
        assert resp["status"] == 404

    async def test_evaluate_task_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                             service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/tasks/no-such/evaluate", "POST", body={})
        assert resp["status"] == 404

    async def test_evaluate_task_body_metric_ids(self, monkeypatch: pytest.MonkeyPatch,
                                                 service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="带指标")
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/tasks/{t.id}/evaluate", "POST",
                           body={"metric_ids": ["m1"]})
        assert resp["status"] == 200
        assert resp["payload"]["overall_passed"] is False

    # ── 创建/提交失败路径 ──

    async def test_create_task_chat_no_pipeline_500(self, monkeypatch: pytest.MonkeyPatch,
                                                    service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {}}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "x", "agent_id": "main"})
        assert resp["status"] == 500
        assert "创建失败" in resp["payload"]["detail"]

    async def test_submit_task_chat_empty_queued(self, monkeypatch: pytest.MonkeyPatch,
                                                 service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {}}
        t = await service.create_task(title="注入失败")
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/tasks/{t.id}/submit", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["status"] == "queued"

    async def test_create_root_non_container_with_target(self, monkeypatch: pytest.MonkeyPatch,
                                                         service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-nc-1"}}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "非容器根", "thread_id": "th-1",
                                 "task_scope": "non_container", "target_id": "main"})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-nc-1"

    async def test_create_root_chat_empty_500(self, monkeypatch: pytest.MonkeyPatch,
                                              service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {}}
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根", "thread_id": "th-1", "task_scope": "container"})
        assert resp["status"] == 500

    async def test_create_root_workspace_unsafe_400(self, monkeypatch: pytest.MonkeyPatch,
                                                    service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根", "thread_id": "th-1", "task_scope": "container",
                                 "workspace": "C:\\"})
        assert resp["status"] == 400
        assert "磁盘根目录" in resp["payload"]["detail"]

    async def test_create_root_workspace_safe_ok(self, monkeypatch: pytest.MonkeyPatch,
                                                 service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-ws-1"}}
        safe = str(Path(tempfile.mkdtemp(prefix="tasks_ws_")))
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "带空间", "thread_id": "th-1",
                                 "task_scope": "container", "workspace": safe})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-ws-1"

    async def test_create_root_parent_not_container_400(self, monkeypatch: pytest.MonkeyPatch,
                                                        service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="非容器父")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "子", "thread_id": "th-1",
                                 "task_scope": "non_container", "target_id": "main",
                                 "parent_task_id": t.id})
        assert resp["status"] == 400
        assert "必须是容器" in resp["payload"]["detail"]

    async def test_submit_event_parent_lineage_branch(self, monkeypatch: pytest.MonkeyPatch,
                                                      service: Any, hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, service, hub)
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-child-1"}}
        pid = await http_api._submit_task_event(
            title="子任务", parent_pipeline_id="p-container", user_id="u-1",
        )
        assert pid == "p-child-1"
        params = hub.handles["chat"].calls[0][1]
        assert params["lineage"]["parent_pipeline_id"] == "p-container"

    # ── 服务不可用（task_service None）──

    async def test_service_unavailable_degradations(self, monkeypatch: pytest.MonkeyPatch,
                                                    service: Any, hub: _FakeCapabilityHub) -> None:
        import http_api

        monkeypatch.setattr(http_api, "_get_task_service", lambda: None)
        monkeypatch.setattr(http_api, "_capability", hub.get)

        # 读面空降级（200 空）
        r = await http_api.handle_http("/ext/task_service/tasks", "GET", "", {}, None)
        assert json.loads(base64.b64decode(r["data"]["body"])) == {"items": [], "total": 0}
        r = await http_api.handle_http("/ext/task_service/tasks/debug/all", "GET", "", {}, None)
        assert json.loads(base64.b64decode(r["data"]["body"])) == {"items": [], "total": 0}
        r = await http_api.handle_http("/ext/task_service/tasks/containers", "GET", "", {}, None)
        assert json.loads(base64.b64decode(r["data"]["body"])) == []
        r = await http_api.handle_http("/ext/task_service/projects", "GET", "", {}, None)
        assert json.loads(base64.b64decode(r["data"]["body"]))["items"] == []

        # 写面 503
        r = await http_api.handle_http("/ext/task_service/tasks", "POST",
                                       json.dumps({"title": "x", "agent_id": "main"}), {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/tasks/root", "POST",
                                       json.dumps({"title": "x", "thread_id": "th"}),
                                       {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/tasks/x", "DELETE", "", {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/tasks/x/cancel", "POST", "", {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/projects", "POST",
                                       json.dumps({"goal": "g"}), {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/projects/x", "GET", "", {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/projects/x/auto-execute", "POST",
                                       "", {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/projects/x/pause", "POST", "", {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/projects/x/resume", "POST", "", {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/projects/x", "DELETE", "", {}, None)
        assert r["data"]["status"] == 503

    # ── update 读 state 聚合边界 ──

    async def test_update_task_503_when_state_unavailable(self, monkeypatch: pytest.MonkeyPatch,
                                                          service: Any, hub: _FakeCapabilityHub) -> None:
        t = await service.create_task(title="u")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}", "PATCH",
                           body={"title": "ignored"})
        assert resp["status"] == 503

    async def test_update_task_404_no_state_row(self, monkeypatch: pytest.MonkeyPatch,
                                                service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-state"] = {"list": []}
        t = await service.create_task(title="u")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{t.id}", "PATCH",
                           body={"title": "ignored"})
        assert resp["status"] == 404

    # ── tasks 域 delete 容器软删消息 ──

    async def test_delete_container_task_soft_message(self, monkeypatch: pytest.MonkeyPatch,
                                                      service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="软删容器")
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{c.id}", "DELETE")
        assert resp["status"] == 200
        assert resp["payload"]["message"] == "容器任务已标记删除"

    # ── phase 映射 default ──

    async def test_phase_status_stopped_default(self, monkeypatch: pytest.MonkeyPatch,
                                                service: Any, hub: _FakeCapabilityHub) -> None:
        c = await _seed_container(service, title="暂停态")
        await service.pause_task(c.id)  # running → stopped（不在映射表 → 默认 prepare）
        resp = await _http(monkeypatch, service, hub, f"/ext/task_service/tasks/{c.id}/phase")
        assert resp["payload"] == {"taskId": c.id, "currentPhase": "prepare",
                                   "phaseStatus": "pending"}

    # ── projects 边界 ──

    async def test_list_projects_bad_limit_fallback(self, monkeypatch: pytest.MonkeyPatch,
                                                    service: Any, hub: _FakeCapabilityHub) -> None:
        await _seed_container(service, title="A")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects",
                           query={"limit": "abc"})
        assert resp["status"] == 200
        assert resp["payload"]["limit"] == 20
        assert resp["payload"]["total"] == 1

    async def test_list_projects_page_offset(self, monkeypatch: pytest.MonkeyPatch,
                                             service: Any, hub: _FakeCapabilityHub) -> None:
        await _seed_container(service, title="P1")
        await _seed_container(service, title="P2")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects",
                           query={"page": "2", "limit": "1"})
        assert resp["payload"]["total"] == 2
        assert len(resp["payload"]["items"]) == 1

    async def test_list_projects_service_failure_empty(self, monkeypatch: pytest.MonkeyPatch,
                                                       hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, _CrashService(), hub, "/ext/task_service/projects")
        assert resp["status"] == 200
        assert resp["payload"]["items"] == []

    async def test_create_project_ws_mode_fallback_and_meta(
            self, monkeypatch: pytest.MonkeyPatch, service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "模式回退", "session_id": "s",
                                 "workspace_mode": "bogus", "workspace": "D:\\ws",
                                 "metadata": {"custom": 7}})
        assert resp["status"] == 200
        proj = resp["payload"]["project"]
        task = service.get_task(proj["id"])
        assert (task.metadata or {}).get("ws_meta", {}).get("mode") == "worktree"
        assert (task.metadata or {}).get("custom") == 7

    async def test_get_project_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                           service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects/no-such")
        assert resp["status"] == 404

    async def test_toggle_auto_execute_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                                   service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub,
                           "/ext/task_service/projects/no-such/auto-execute", "POST", body={})
        assert resp["status"] == 404

    async def test_pause_project_state_machine_reject(self, monkeypatch: pytest.MonkeyPatch,
                                                      service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        c = await _seed_container(service, title="状态机拒绝")

        async def _boom(task_id: str, paused_by: str = "user") -> None:
            raise RuntimeError("state machine refused")

        monkeypatch.setattr(service, "pause_task", _boom)
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/projects/{c.id}/pause", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["id"] == c.id

    async def test_resume_project_state_machine_reject(self, monkeypatch: pytest.MonkeyPatch,
                                                       service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"resume_pipeline": {"run_id": "r-2"}}
        c = await _seed_container(service, title="恢复拒绝")
        await service.pause_task(c.id)  # → stopped，resume 走状态机

        async def _boom(task_id: str) -> Any:
            raise RuntimeError("state machine refused")

        monkeypatch.setattr(service, "resume_task", _boom)
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/projects/{c.id}/resume", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["id"] == c.id

    async def test_delete_project_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                              service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/projects/no-such",
                           "DELETE")
        assert resp["status"] == 404

    # ── 分发兜底 ──

    async def test_ac_unknown_subroute_404(self, monkeypatch: pytest.MonkeyPatch,
                                           service: Any, hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/t-1/ac/unknown")
        assert resp["status"] == 404

    async def test_handle_unexpected_error_500(self, monkeypatch: pytest.MonkeyPatch,
                                               service: Any, hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, service, hub)

        # get_task 是同步 handler（dispatcher 不 await 单级详情）
        def _boom(task_id: str, _user: Any = None) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(http_api, "get_task", _boom)
        result = await http_api.handle_http("/ext/task_service/tasks/x", "GET", "", {}, None)
        assert result["data"]["status"] == 500
        payload = json.loads(base64.b64decode(result["data"]["body"]).decode("utf-8"))
        assert payload["error"] == "internal server error"


# ═══════════════════════════════════════════════════════════
# 7. 覆盖收尾（http_api.py 剩余可及语句）
# ═══════════════════════════════════════════════════════════


class TestCoverageRemainingBranches:
    def test_pydantic_to_dict_non_model(self) -> None:
        import http_api

        assert http_api._pydantic_to_dict({"a": 1}) == {"a": 1}

    def test_resolve_caller_malformed_token(self) -> None:
        import http_api

        assert http_api._resolve_caller(
            {"Authorization": "Bearer !!!not-base64!!!"}
        ) == {}

    def test_task_to_response_agent_level_value(self) -> None:
        import http_api
        import types

        out = http_api._task_to_response({
            "id": "x",
            "title": "t",
            "status": "pending",
            "metadata": {"agent_level": types.SimpleNamespace(value="L9")},
        })
        assert out.agent_level == "L9"

    async def test_submit_event_description_and_exception_branches(
            self, monkeypatch: pytest.MonkeyPatch, service: Any, hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, service, hub)
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-desc"}}
        # 注入模式带描述（覆盖 444）
        pid = await http_api._submit_task_event(
            title="t", description="注入描述", task_id="t-1",
        )
        assert pid == "t-1"
        # 创建模式带描述（覆盖 480）
        pid2 = await http_api._submit_task_event(title="t", description="创建描述")
        assert pid2 == "p-desc"
        # chat 能力缺失 → 派发异常降级空串（覆盖 519-521）
        hub2 = _FakeCapabilityHub({})
        monkeypatch.setattr(http_api, "_capability", hub2.get)
        assert await http_api._submit_task_event(title="t") == ""

    async def test_list_tasks_skip_override(self, monkeypatch: pytest.MonkeyPatch,
                                            service: Any, hub: _FakeCapabilityHub) -> None:
        await service.create_task(title="A")
        await service.create_task(title="B")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks",
                           query={"skip": "1", "limit": "1"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 2
        assert len(resp["payload"]["items"]) == 1

    async def test_direct_handler_dict_body_compat(self, monkeypatch: pytest.MonkeyPatch,
                                                   service: Any, hub: _FakeCapabilityHub) -> None:
        """dict 直调兼容：SDK 按 dict 透传 body 时 handler 自行实例化模型。"""
        import http_api

        _install(monkeypatch, service, hub)
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-d1"}}
        t = await http_api.create_task({"title": "直调", "agent_id": "main"}, {})
        assert t.id == "p-d1"
        r = await http_api.create_root_task(
            {"title": "直调根", "thread_id": "th", "task_scope": "container"}, {},
        )
        assert r.id == "p-d1"
        hub._responses["pipeline-state"] = {
            "list": [{"pipeline_id": "p-9", "task.goal": "G", "task.status": "running"}],
        }
        u = await http_api.update_task("p-9", {"title": "ignored"}, {})
        assert u.id == "p-9"
        assert u.title == "G"

    async def test_create_root_with_container_parent(self, monkeypatch: pytest.MonkeyPatch,
                                                     service: Any, hub: _FakeCapabilityHub) -> None:
        hub._responses["chat"] = {"send_message": {"pipeline_id": "p-child-9"}}
        parent = await _seed_container(service, title="父容器")
        resp = await _http(monkeypatch, service, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "容器子", "thread_id": "th-1",
                                 "task_scope": "container", "parent_task_id": parent.id})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-child-9"

    async def test_evaluate_task_dict_body(self, monkeypatch: pytest.MonkeyPatch,
                                           service: Any, hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, service, hub)
        t = await service.create_task(title="直评")
        # evaluate_task 为同步 handler（源实现同款）
        out = http_api.evaluate_task(t.id, {"metric_ids": ["m1"]}, {})
        assert out.overall_passed is False
        assert out.summary == "评估引擎不可用"

    async def test_cancel_task_agent_level_value(self, monkeypatch: pytest.MonkeyPatch,
                                                 service: Any, hub: _FakeCapabilityHub) -> None:
        from agents_types import AgentLevel  # noqa: PLC0415

        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        t = await service.create_task(title="带层级", agent_level=AgentLevel.L2_SUBTASK)
        resp = await _http(monkeypatch, service, hub,
                           f"/ext/task_service/tasks/{t.id}/cancel", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["agent_level"] == "L2"

    async def test_phase_service_exception_fallback(self, monkeypatch: pytest.MonkeyPatch,
                                                    service: Any, hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, service, hub)

        def _boom(task_id: str) -> Any:
            raise RuntimeError("storage boom")

        monkeypatch.setattr(service, "get_task", _boom)
        result = await http_api.handle_http(
            "/ext/task_service/tasks/t-1/phase", "GET", "", {}, None,
        )
        payload = json.loads(base64.b64decode(result["data"]["body"]).decode("utf-8"))
        assert payload == {"taskId": "t-1", "currentPhase": "prepare",
                           "phaseStatus": "pending"}

    async def test_handle_http_status_code_exception(self, monkeypatch: pytest.MonkeyPatch,
                                                     service: Any, hub: _FakeCapabilityHub) -> None:
        """最终 except 的 status_code 分支（非 APIError 但带状态码的业务异常）。"""
        import http_api

        _install(monkeypatch, service, hub)

        class _HttpErr(Exception):
            status_code = 404
            detail = "gone-ish"

        def _boom(task_id: str, _user: Any = None) -> Any:
            raise _HttpErr()

        monkeypatch.setattr(http_api, "get_task", _boom)
        result = await http_api.handle_http("/ext/task_service/tasks/x", "GET", "", {}, None)
        assert result["data"]["status"] == 404
        payload = json.loads(base64.b64decode(result["data"]["body"]).decode("utf-8"))
        assert payload == {"detail": "gone-ish"}