# @feature: channel_api 拆迁批次1/3 | @ci: python-coverage
"""tasks 插件自持 HTTP 面测试（http_api.handle_http 分发 + projects 接真生命周期）。

测试置于 tests/plugins/system/（插桩车道）：tasks 插件整体在 plugins-heavy
免插桩豁免名单内（scripts/coverage_exempt.py EXEMPT_SUITES），插件目录内
test_*.py 不产生 coverage.xml 度量；本套件迁入此目录后 http_api.py 才进入
改动行覆盖率门禁（check_diff_coverage.py）可度量面，与 workspace/artifacts
拆迁批次测试同构。

覆盖（2026-08-29 面板创建工具化 + 读面 state 单一真值后的契约）：

1. manifest：plugin.json http_endpoints 27 条声明（命名空间/枚举/auth/timeout）。
2. 分发：handle_http 全部路径→handler 接线（27 端点形状走通，含 404/400 语义）。
3. tasks 域：创建（= task_submit 工具的表单提交，经 tool-executor.invoke，
   人类注入 parent_agent_level=1）/列表（state 聚合行，status/session 筛选）/
   详情/删除（delete_pipeline）/提交（state 状态门 + 注入重跑）/评估（退役 410）/
   暂停/恢复/取消（管道 fake + 级联）/根任务（字段透传）/update 读 state 聚合。
   任务域唯一数据源 = 管道 state，无 YAML 兜底面。
4. phase/ac 域：state 行状态→阶段映射 + 占位语义。
5. projects 域接真：创建项目 = 文件夹 + project_registry 登记（非任务实体），
   list/get/pause/resume/auto-execute/delete 项目生命周期；响应形状对齐
   frontend/src/types/task.ts Project。
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

    teardown 还原逐出前的模块代际（与 test_tasks_plugin.py 同款）：本插件
    运行期 import 的 http_api/server 等模块若不还原，会以本插件版本残留
    sys.modules，污染后续测试文件的运行期惰性导入（triggers_ext/server.py
    的 ``from http_api import handle_http_dispatch`` 曾命中 tasks 版
    http_api 而 ImportError）。
    """
    d = str(_PLUGIN_DIR)
    # 共享根（plugins/shared 平铺模块：state_fields/task_birth/project_registry 等）
    # ——对齐生产 sidecar sys.path 形态（自身目录 + shared 根），单文件可跑。
    shared_root = str(_PLUGIN_DIR.parents[1])
    _was_present = d in sys.path
    _added_shared_root = False
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    if shared_root not in sys.path:
        sys.path.insert(1, shared_root)
        _added_shared_root = True
    _evict_names = (
        "task_types",
        "state_machine",
        "storage",
        "service",
        "timer_manager",
        "agents_types",
        "enum_utils",
        "workspace",
        "service_access",
        "project_registry",
        "_task_cleanup",
        "_task_crud",
        "_task_state",
        "http_api",
        "server",
    )
    _evicted: dict[str, object] = {}
    for m in _evict_names:
        if m in sys.modules:
            _evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if _was_present:
        sys.path.insert(0, d)
    if _added_shared_root:
        sys.path.remove(shared_root)
    for m in _evict_names:
        if m in _evicted:
            sys.modules[m] = _evicted[m]
        else:
            sys.modules.pop(m, None)


# ═══════════════════════════════════════════════════════════
# 测试脚手架
# ═══════════════════════════════════════════════════════════


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应（未预置 → KeyError 语义）。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
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
            self.handles[name] = _FakeCapability(self._responses.get(name, {}))
        return self.handles[name]


@pytest.fixture
def hub() -> _FakeCapabilityHub:
    return _FakeCapabilityHub({})


def _install(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
    """注入能力句柄（http_api 内部引用点；任务域无 YAML 服务引用）。"""
    import http_api

    monkeypatch.setattr(http_api, "_capability", hub.get)


def _seed_state(hub: _FakeCapabilityHub, rows: list[dict[str, Any]]) -> None:
    """播种 state 聚合原始行（pipeline-state.list 响应）。"""
    hub._responses["pipeline-state"] = {"list": rows}


def _tool_ok(task_id: str, **extra: Any) -> dict[str, Any]:
    """task_submit 工具成功信封（SDK ToolExecutionResult to_dict 形状）。"""
    return {
        "success": True,
        "output": {"task_id": task_id, "pipeline_id": task_id, **extra},
    }


async def _http(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
                path: str, method: str = "GET", body: dict[str, Any] | None = None,
                query: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """走完整 handle_http 分发，返回解码后的 HTTP 响应（{status, payload}）。"""
    import http_api

    _install(monkeypatch, hub)
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


def _state_row(pid: str, title: str, status: str, **extra: Any) -> dict[str, Any]:
    """构造一条任务执行管道 state 聚合原始行（task.* 扁平键）。"""
    row = {
        "pipeline_id": pid,
        "task.goal": title,
        "task.status": status,
        "task.submitted_by": "u-1",
        "lineage.origin_session_id": extra.pop("session", f"sess-{pid}"),
        "thread_id": pid,
    }
    row.update(extra)
    return row


# ═══════════════════════════════════════════════════════════
# 1. manifest 声明
# ═══════════════════════════════════════════════════════════

class TestManifestHttpEndpoints:
    """plugin.json http_endpoints 声明与分发语义一致。"""

    def test_declares_27_endpoints(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        eps = manifest["http_endpoints"]
        assert len(eps) == 27, f"tasks(20)+projects(7)=27（containers 已随容器任务退役），实际 {len(eps)}"

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
        assert ("GET", "/ext/task_service/tasks/containers") not in paths  # 容器端点已退役
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
    async def test_unknown_path_404(self, monkeypatch: pytest.MonkeyPatch,
                                    hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/nope")
        assert resp["status"] == 404

    async def test_outside_namespace_404(self, monkeypatch: pytest.MonkeyPatch,
                                         hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/other/tasks")
        assert resp["status"] == 404

    async def test_invalid_json_body_400(self, monkeypatch: pytest.MonkeyPatch,
                                         hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, hub)
        result = await http_api.handle_http(
            "/ext/task_service/tasks", "POST", "not-json-at-all", {}, None,
        )
        assert result["data"]["status"] == 400


# ═══════════════════════════════════════════════════════════
# 3. tasks 域端点
# ═══════════════════════════════════════════════════════════

class TestTasksEndpoints:
    async def test_create_task_maps_and_returns_tool_result(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """面板创建无本地预检：必填闸门在 task_submit 工具侧（LLM 提交同律），
        本端点只做映射与结果转写（映射断言见 test_create_task_via_task_submit_tool，
        拒绝信封映射见 test_create_task_tool_rejection_maps_error_code）。"""
        hub._responses["tool-executor"] = {"invoke": _tool_ok("p-created-1", status="running")}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "测试任务", "description": "做一件事", "agent_id": "main"})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-created-1"

    async def test_create_task_via_task_submit_tool(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """面板创建 = 工具表单提交：映射工具入参 + 人类注入参数，回写响应。"""
        hub._responses["tool-executor"] = {"invoke": _tool_ok("p-created-1", status="running")}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "测试任务", "description": "做一件事", "agent_id": "main"})
        assert resp["status"] == 200
        t = resp["payload"]
        assert t["id"] == "p-created-1"
        assert t["title"] == "测试任务"
        invoke_method, invoke_params = hub.handles["tool-executor"].calls[0]
        assert invoke_method == "invoke"
        assert invoke_params["tool_name"] == "task_submit"
        args = invoke_params["args"]
        assert args["goal_title"] == "测试任务"
        assert args["goal_description"] == "做一件事"
        assert args["target_type"] == "agent"
        assert args["target_id"] == "main"
        assert args["parent_agent_level"] == 1  # 人类 = L1 之上

    async def test_create_task_tool_rejection_maps_error_code(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """工具拒绝信封 → error_code 透传 + detail 为工具错误消息。"""
        hub._responses["tool-executor"] = {"invoke": {
            "success": False, "error": "目标 Agent 不存在", "error_code": "TARGET_AGENT_NOT_FOUND",
        }}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "x", "description": "d", "agent_id": "ghost"})
        assert resp["status"] == 400
        assert "目标 Agent 不存在" in resp["payload"]["detail"]

    async def test_list_tasks_state_rows(self, monkeypatch: pytest.MonkeyPatch,
                                         hub: _FakeCapabilityHub) -> None:
        """列表 = state 聚合任务行（无 task.* 键的会话管道不是任务）。"""
        _seed_state(hub, [
            _state_row("pipe-0-2-task", "0.2 提交的任务", "running"),
            # 无 task.* 字段的普通会话管道 → 不是任务，跳过
            {"pipeline_id": "pipe-session", "status": "active", "ended": False},
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks")
        assert resp["status"] == 200
        items = resp["payload"]["items"]
        assert resp["payload"]["total"] == 1
        t = items[0]
        assert t["id"] == "pipe-0-2-task"
        assert t["title"] == "0.2 提交的任务"
        assert t["status"] == "running"
        assert t["pipeline_run_id"] == "pipe-0-2-task"
        # 会话锚点：lineage.origin_session_id 为真 thread id（非自身 pipeline_id）
        assert t["thread_id"] == "sess-pipe-0-2-task"

    async def test_list_tasks_ws_meta_prefers_task_mirror(self, monkeypatch: pytest.MonkeyPatch,
                                                          hub: _FakeCapabilityHub) -> None:
        """任务域镜像 task.ws_meta 优先于裸 ws_meta：任务管道的裸键会被会话
        工作区投影污染成会话目录，打开工作空间不能落到会话默认文件夹。"""
        _seed_state(hub, [
            _state_row(
                "pipe-ws", "带污染键的任务", "running",
                ws_meta={"mode": "plain", "path": "D:/ws/sessions/thread-x",
                         "session_id": "thread-x"},
                **{"task.ws_meta": {"mode": "worktree", "path": "D:/ws/proj__wt_1",
                                    "project_root": "D:/ws/proj"}},
            ),
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks")
        t = resp["payload"]["items"][0]
        assert t["metadata"]["ws_meta"]["path"] == "D:/ws/proj__wt_1"

    async def test_list_tasks_ws_meta_fallback_to_bare_key(self, monkeypatch: pytest.MonkeyPatch,
                                                           hub: _FakeCapabilityHub) -> None:
        """无任务镜像（主会话形态行）→ 回退裸 ws_meta，行为不变。"""
        _seed_state(hub, [
            _state_row(
                "pipe-plain", "仅有裸键的任务", "running",
                ws_meta={"mode": "plain", "path": "D:/ws/plain-dir"},
            ),
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks")
        t = resp["payload"]["items"][0]
        assert t["metadata"]["ws_meta"]["path"] == "D:/ws/plain-dir"

    async def test_list_tasks_state_unavailable_empty(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """pipeline-state 能力不可用 → 空列表 200（state 是唯一数据源，无兜底面）。"""
        # hub 无 pipeline-state 预置 → fake call KeyError → 读面降级空
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks")
        assert resp["status"] == 200
        assert resp["payload"] == {"items": [], "total": 0}

    async def test_list_tasks_state_status_filter(self, monkeypatch: pytest.MonkeyPatch,
                                                  hub: _FakeCapabilityHub) -> None:
        """state 行参与 status 筛选（pending_evaluation 等细态原样透传）。"""
        _seed_state(hub, [
            _state_row("pipe-eval", "待评估任务", "pending_evaluation"),
            _state_row("pipe-run", "执行中", "running"),
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks",
                           query={"status": "pending_evaluation"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["status"] == "pending_evaluation"

    async def test_list_tasks_session_filter(self, monkeypatch: pytest.MonkeyPatch,
                                             hub: _FakeCapabilityHub) -> None:
        """session_id 筛选基于会话锚点（metadata.session_id）。"""
        _seed_state(hub, [
            _state_row("pipe-a", "S", "pending", session="sess-a"),
            _state_row("pipe-b", "O", "pending", session="sess-b"),
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks",
                           query={"session_id": "sess-a"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["title"] == "S"

    # ── 登记型任务：容器任务 = 提交者管道自持（task.owned.*）──

    async def test_list_tasks_merges_owned_registered_tasks(self, monkeypatch: pytest.MonkeyPatch,
                                                           hub: _FakeCapabilityHub) -> None:
        """登记型任务（task.owned.<id>.* 键，如 review 复盘管道）从提交者管道聚合行组装。"""
        _seed_state(hub, [
            {
                "pipeline_id": "owner-pipe-1",
                "task.owned.c1d2e3f4a5b6.title": "复盘 task-9",
                "task.owned.c1d2e3f4a5b6.status": "running",
                "task.owned.c1d2e3f4a5b6.submitted_by": "review_system",
                "lineage.origin_session_id": "sess-owner",
                "thread_id": "owner-pipe-1",
            }
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks")
        assert resp["status"] == 200
        items = resp["payload"]["items"]
        assert resp["payload"]["total"] == 1
        t = items[0]
        assert t["id"] == "c1d2e3f4a5b6"
        assert t["title"] == "复盘 task-9"
        assert t["status"] == "running"
        # 登记型任务无执行管道：pipeline_run_id 指向提交者管道（归属定位用）
        assert t["pipeline_run_id"] == "owner-pipe-1"
        assert t["thread_id"] == "sess-owner"

    async def test_get_task_ok_and_404(self, monkeypatch: pytest.MonkeyPatch,
                                       hub: _FakeCapabilityHub) -> None:
        """详情与列表同源：state 行命中 200，未命中 404（无 YAML 兜底）。"""
        _seed_state(hub, [_state_row("pipe-detail-1", "详情", "completed")])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/pipe-detail-1")
        assert resp["status"] == 200
        assert resp["payload"]["title"] == "详情"
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/missing-1")
        assert resp["status"] == 404

    async def test_delete_task(self, monkeypatch: pytest.MonkeyPatch,
                               hub: _FakeCapabilityHub) -> None:
        """删除 = state 存在性判定 + delete_pipeline 级联清数据。"""
        _seed_state(hub, [_state_row("pipe-del-1", "删", "failed")])
        hub._responses["pipeline-executor"] = {"delete_pipeline": {"deleted": True}}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/pipe-del-1", "DELETE")
        assert resp["status"] == 200
        assert resp["payload"]["message"] == "任务已删除"
        calls = hub.handles["pipeline-executor"].calls
        assert calls[0][0] == "delete_pipeline"
        assert calls[0][1]["pipeline_id"] == "pipe-del-1"
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/missing-1", "DELETE")
        assert resp["status"] == 404

    async def test_submit_task_state_gate(self, monkeypatch: pytest.MonkeyPatch,
                                          hub: _FakeCapabilityHub) -> None:
        """重跑状态门读 state 行：pending 可提交（注入模式），running 拒绝。"""
        _seed_state(hub, [
            _state_row("pipe-retry-1", "提交", "pending"),
            _state_row("pipe-run-1", "运行中", "running"),
        ])
        hub._responses["chat"] = {"send_message": {"pipeline_id": "pipe-retry-1"}}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/pipe-retry-1/submit",
                           "POST")
        assert resp["status"] == 200
        assert resp["payload"]["task_id"] == "pipe-retry-1"
        # 注入派发走 chat.send_message 注入分支
        chat_call = hub.handles["chat"].calls[0]
        assert chat_call[0] == "send_message"
        assert chat_call[1]["pipeline_id"] == "pipe-retry-1"
        assert chat_call[1]["background"] is True

        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/pipe-run-1/submit",
                           "POST")
        assert resp["status"] == 400

    @pytest.mark.parametrize(
        "task_exists",
        [True, False],
        ids=["existing-task", "missing-task"],
    )
    async def test_evaluate_endpoint_retired_410(self, monkeypatch: pytest.MonkeyPatch,
                                                 hub: _FakeCapabilityHub,
                                                 task_exists: bool) -> None:
        """评估端点退役契约：任何 POST /tasks/{id}/evaluate 恒 410 并指引 task_evaluate 工具。

        0.2 评估闸门已插件化（task_evaluate 工具承载），HTTP 面不再提供评估，
        也不允许"引擎不可用"式降级假成功；任务存在与否不改变 410 响应。
        """
        tid = "pipe-eval-1" if task_exists else "no-such-task"
        resp = await _http(monkeypatch, hub,
                           f"/ext/task_service/tasks/{tid}/evaluate", "POST", body={})
        assert resp["status"] == 410
        assert "task_evaluate" in resp["payload"]["detail"]

    async def test_evaluate_with_metric_ids_still_410_no_fake_payload(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """带指标体的评估请求同样 410，且响应不再携带伪造的评估结果字段。"""
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/pipe-eval-1/evaluate", "POST",
                           body={"metric_ids": ["m1"]})
        assert resp["status"] == 410
        # 防降级假成功回归：旧死分支会返回 overall_passed/summary/results 信封
        assert "overall_passed" not in resp["payload"]
        assert "summary" not in resp["payload"]
        assert "results" not in resp["payload"]

    async def test_pause_resume_task(self, monkeypatch: pytest.MonkeyPatch,
                                     hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "r-1"},
            "resume_pipeline": {"run_id": "r-2"},
        }
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/pause", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["success"] is True
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/resume", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["resumed_count"] == 1

    async def test_resume_task_passes_caller_user_to_kernel(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        # 恢复续跑轮的租户/归属解析依赖调用方 user——resume_pipeline 调用
        # 必须携带 user_id（缺省空串走内核 default 租户降级，带认证时须是真值）。
        hub._responses["pipeline-executor"] = {"resume_pipeline": {"run_id": "r-9"}}
        headers = {"Authorization": f"Bearer {_make_token('u-9')}"}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/resume", "POST",
                           headers=headers)
        assert resp["status"] == 200
        calls = [(m, pr) for m, pr in hub.handles["pipeline-executor"].calls
                 if m == "resume_pipeline"]
        assert calls[0][1]["user_id"] == "u-9"

    async def test_pause_task_no_run_404(self, monkeypatch: pytest.MonkeyPatch,
                                         hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {}}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/pause", "POST")
        assert resp["status"] == 404

    async def test_cancel_task_cascade(self, monkeypatch: pytest.MonkeyPatch,
                                       hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        _seed_state(hub, [
            {"pipeline_id": "pipe-child", "lineage.parent_pipeline_id": "pipe-parent",
             "task.goal": "子", "task.status": "running"},
        ])
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/pipe-parent/cancel", "POST")
        assert resp["status"] == 200
        # 取消语义：success + 级联计数（状态真值在 state，响应不回拼任务 dict）
        assert resp["payload"]["cancelled"] is True
        assert resp["payload"]["cascaded_subtasks"] == 1
        # 级联挂起：父管道 + lineage 子管道都在 suspend_pipeline 调用里
        suspend_ids = [p["pipeline_id"] for m, p in hub.handles["pipeline-executor"].calls
                       if m == "suspend_pipeline"]
        assert "pipe-parent" in suspend_ids
        assert "pipe-child" in suspend_ids

    async def test_create_root_task_maps_fields_to_tool(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            registry: Any, tmp_path: Path) -> None:
        """根任务表单字段机械透传工具入参（project/workspace/workspace_mode/thread）。"""
        import project_registry as projects_mod

        folder = tmp_path / "proj"
        folder.mkdir()
        p = registry.save(projects_mod.ProjectModel(title="挂靠项目", path=str(folder)))
        hub._responses["tool-executor"] = {"invoke": _tool_ok("p-root-1")}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "项目任务", "description": "d", "thread_id": "th-1",
                                 "project_id": p.id, "target_id": "main",
                                 "workspace_mode": "plain", "isolation_level": "isolated"})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-root-1"
        _, invoke_params = hub.handles["tool-executor"].calls[0]
        args = invoke_params["args"]
        assert args["project_id"] == p.id
        assert args["workspace_mode"] == "plain"  # 前端契约字段（此前被静默丢弃）
        assert args["isolation_level"] == "isolated"
        assert args["thread_id"] == "th-1"  # 会话归属锚点透传
        assert args["parent_agent_level"] == 1

    async def test_update_task_reads_state_rows(self, monkeypatch: pytest.MonkeyPatch,
                                                hub: _FakeCapabilityHub) -> None:
        _seed_state(hub, [_state_row("p-9", "目标九", "running")])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/p-9", "PATCH",
                           body={"title": "ignored"})
        assert resp["status"] == 200
        assert resp["payload"]["id"] == "p-9"
        assert resp["payload"]["title"] == "目标九"
        assert resp["payload"]["status"] == "running"

    async def test_get_tasks_debug(self, monkeypatch: pytest.MonkeyPatch,
                                   hub: _FakeCapabilityHub) -> None:
        """调试面同源 state：status/session_id 筛选 + created_at 排序。"""
        _seed_state(hub, [
            _state_row("pipe-d1", "d1", "pending", session="s"),
            _state_row("pipe-d2", "d2", "running", session="s"),
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/debug/all",
                           query={"status": "running"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["title"] == "d2"
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/debug/all",
                           query={"session_id": "s"})
        assert resp["payload"]["total"] == 2


# ═══════════════════════════════════════════════════════════
# 4. task_phase / ac 域端点
# ═══════════════════════════════════════════════════════════

class TestPhaseAndAceEndpoints:
    async def test_get_task_phase_mapping(self, monkeypatch: pytest.MonkeyPatch,
                                          hub: _FakeCapabilityHub) -> None:
        _seed_state(hub, [_state_row("pipe-phase-1", "阶段", "running")])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/pipe-phase-1/phase")
        assert resp["payload"]["currentPhase"] == "execute"
        assert resp["payload"]["phaseStatus"] == "running"

    async def test_get_task_phase_missing_task_default(self, monkeypatch: pytest.MonkeyPatch,
                                                       hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/missing/phase")
        assert resp["payload"]["currentPhase"] == "prepare"
        assert resp["payload"]["phaseStatus"] == "pending"

    async def test_complete_phases(self, monkeypatch: pytest.MonkeyPatch,
                                   hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/t-1/phase/prepare/complete", "POST")
        assert resp["payload"]["current_phase"] == "execute"
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/t-1/phase/execute/complete", "POST")
        assert resp["payload"]["current_phase"] == "review"

    async def test_phase_output(self, monkeypatch: pytest.MonkeyPatch,
                                hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/t-1/phase/execute/output")
        assert resp["payload"] == {"output": None, "error": None}

    async def test_ac_endpoints_shapes(self, monkeypatch: pytest.MonkeyPatch,
                                       hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/ac")
        assert resp["payload"] == {"taskId": "t-1", "acceptanceCriteria": []}
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/t-1/ac/evaluate-all", "POST")
        assert resp["payload"] == {"taskId": "t-1", "acceptanceCriteria": []}
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/t-1/ac/ac-1/evaluate", "POST")
        assert resp["payload"]["acceptance_criterion"]["status"] == "not_evaluated"
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/ac/ac-1/result")
        assert resp["payload"]["acceptance_criterion"]["passed"] is None

    async def test_phase_unknown_subroute_404(self, monkeypatch: pytest.MonkeyPatch,
                                              hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/t-1/phase/unknown/action", "POST")
        assert resp["status"] == 404


# ═══════════════════════════════════════════════════════════
# 5. projects 域（project = 文件夹 + 登记行，非任务实体）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """临时目录 ProjectRegistry + 注入 http_api（避免触碰真实数据目录）。

    同时 patch 共享层 load_project_paths（task_submit 工具的登记解析通道）。
    """
    import project_registry as projects_mod

    reg = projects_mod.ProjectRegistry(data_dir=tmp_path / "tasks")
    monkeypatch.setattr(
        projects_mod,
        "load_project_paths",
        lambda: {p.id: p.path for p in reg.list()},
    )
    import http_api

    monkeypatch.setattr(http_api, "get_project_registry", lambda: reg)
    return reg


class TestProjectsLifecycle:
    async def test_create_project_folder_and_registration(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            registry: Any, tmp_path: Path) -> None:
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        folder = tmp_path / "proj"
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "长期目标", "path": str(folder),
                                 "session_id": "sess-1", "auto_execute": True},
                           headers=headers)
        assert resp["status"] == 200
        proj = resp["payload"]["project"]
        assert proj["goal"] == "长期目标"
        assert proj["userId"] == "u-1"
        assert proj["sessionId"] == "sess-1"
        assert proj["autoExecute"] is True
        assert proj["status"] == "running"
        assert proj["timestamps"]["createdAt"]
        # 文件夹真实建成 + git 初始化（子任务 worktree 前提）
        assert folder.is_dir()
        assert (folder / ".git").exists()
        # 登记行持久化，path 回显
        assert registry.get(proj["id"]).path == str(folder)
        assert proj["metadata"]["path"] == str(folder)

    async def test_create_project_non_git_nonempty_folder_auto_inits(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            registry: Any, tmp_path: Path) -> None:
        folder = tmp_path / "occupied"
        folder.mkdir()
        (folder / "keep.txt").write_text("x", encoding="utf-8")
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "目标", "path": str(folder)})
        # 非空非 git 目录不再拒绝：自动 git init 复用（幂等不删既有文件）
        assert resp["status"] == 200
        assert (folder / "keep.txt").read_text(encoding="utf-8") == "x"
        assert (folder / ".git").is_dir()

    async def test_create_project_requires_goal(self, monkeypatch: pytest.MonkeyPatch,
                                                hub: _FakeCapabilityHub,
                                                registry: Any) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects", "POST", body={})
        assert resp["status"] == 400
        assert "必须指定 goal" in resp["payload"]["detail"]

    async def test_list_projects(self, monkeypatch: pytest.MonkeyPatch,
                                 hub: _FakeCapabilityHub, registry: Any) -> None:
        import project_registry as projects_mod

        registry.save(projects_mod.ProjectModel(title="项目A", submitted_by="u-1"))
        registry.save(projects_mod.ProjectModel(title="项目B", auto_execute=True))
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects",
                           query={"limit": "20"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 2
        assert resp["payload"]["limit"] == 20
        assert {p["goal"] for p in resp["payload"]["items"]} == {"项目A", "项目B"}

    async def test_list_projects_status_filter(self, monkeypatch: pytest.MonkeyPatch,
                                               hub: _FakeCapabilityHub,
                                               registry: Any) -> None:
        import project_registry as projects_mod

        registry.save(projects_mod.ProjectModel(title="暂停的", status="paused"))
        registry.save(projects_mod.ProjectModel(title="运行中"))
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects",
                           query={"status": "suspended"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["goal"] == "暂停的"

    async def test_get_project_with_subtasks(self, monkeypatch: pytest.MonkeyPatch,
                                             hub: _FakeCapabilityHub,
                                             registry: Any) -> None:
        import project_registry as projects_mod

        p = registry.save(projects_mod.ProjectModel(title="项目C"))
        _seed_state(hub, [
            {"pipeline_id": "child-1", "task.parent_project_id": p.id,
             "task.goal": "子任务", "task.status": "running"},
            {"pipeline_id": "other", "task.parent_project_id": "别家"},
        ])
        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{p.id}")
        assert resp["status"] == 200
        proj = resp["payload"]["project"]
        assert proj["id"] == p.id
        assert [t["id"] for t in proj["tasks"]] == ["child-1"]
        assert proj["tasks"][0]["title"] == "子任务"

    async def test_get_project_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                           hub: _FakeCapabilityHub,
                                           registry: Any) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects/no-such")
        assert resp["status"] == 404

    async def test_pause_resume_project_suspends_children(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            registry: Any) -> None:
        import project_registry as projects_mod

        p = registry.save(projects_mod.ProjectModel(title="项目D"))
        _seed_state(hub, [
            {"pipeline_id": "child-1", "task.parent_project_id": p.id},
        ])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "r-1"},
            "resume_pipeline": {"run_id": "r-2"},
        }
        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{p.id}/pause",
                           "POST")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["status"] == "suspended"
        # 名下子任务管道被挂起（可观测副作用）
        susp_calls = [(m, pr) for m, pr in hub.handles["pipeline-executor"].calls
                      if m == "suspend_pipeline"]
        assert [pr["pipeline_id"] for _, pr in susp_calls] == ["child-1"]
        assert registry.get(p.id).status == "paused"

        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{p.id}/resume",
                           "POST")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["status"] == "running"
        res_calls = [(m, pr) for m, pr in hub.handles["pipeline-executor"].calls
                     if m == "resume_pipeline"]
        assert [pr["pipeline_id"] for _, pr in res_calls] == ["child-1"]
        assert registry.get(p.id).status == "active"

    async def test_pause_project_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                             hub: _FakeCapabilityHub,
                                             registry: Any) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects/no-such/pause",
                           "POST")
        assert resp["status"] == 404

    async def test_toggle_auto_execute(self, monkeypatch: pytest.MonkeyPatch,
                                       hub: _FakeCapabilityHub, registry: Any) -> None:
        import project_registry as projects_mod

        p = registry.save(projects_mod.ProjectModel(title="项目E"))  # auto_execute 默认 False
        resp = await _http(monkeypatch, hub,
                           f"/ext/task_service/projects/{p.id}/auto-execute", "POST",
                           body={"enabled": True})
        assert resp["status"] == 200
        assert resp["payload"]["project"]["autoExecute"] is True
        assert registry.get(p.id).auto_execute is True
        # 缺省 enabled → 翻转现值
        resp = await _http(monkeypatch, hub,
                           f"/ext/task_service/projects/{p.id}/auto-execute", "POST", body={})
        assert resp["payload"]["project"]["autoExecute"] is False

    async def test_delete_project_removes_registration_only(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            registry: Any, tmp_path: Path) -> None:
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        folder = tmp_path / "delproj"
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "待删项目", "path": str(folder)}, headers=headers)
        pid = resp["payload"]["project"]["id"]

        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{pid}",
                           "DELETE", headers=headers)
        assert resp["status"] == 200
        assert resp["payload"]["id"] == pid
        assert resp["payload"]["folder_removed"] is False
        assert registry.get(pid) is None
        assert folder.is_dir()  # 未显式 delete_files 时文件夹保留
        # 重删 404（登记已删）
        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{pid}",
                           "DELETE", headers=headers)
        assert resp["status"] == 404

    async def test_delete_project_with_files(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            registry: Any, tmp_path: Path) -> None:
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        folder = tmp_path / "delfiles"
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "连文件夹删", "path": str(folder)}, headers=headers)
        pid = resp["payload"]["project"]["id"]

        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{pid}",
                           "DELETE", query={"delete_files": "true"}, headers=headers)
        assert resp["status"] == 200
        assert resp["payload"]["folder_removed"] is True
        assert not folder.exists()
        assert registry.get(pid) is None


# ═══════════════════════════════════════════════════════════
# 6. 边界与降级路径（http_api.py 剩余可及分支）
# ═══════════════════════════════════════════════════════════


class TestEdgeAndDegradedBranches:
    # ── 分页/列表筛选边界 ──

    async def test_list_tasks_pagination_validation(self, monkeypatch: pytest.MonkeyPatch,
                                                    hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks",
                           query={"limit": "0"})
        assert resp["status"] == 400
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks",
                           query={"limit": "500"})
        assert resp["status"] == 400
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks",
                           query={"offset": "-1"})
        assert resp["status"] == 400

    async def test_list_tasks_skip_override(self, monkeypatch: pytest.MonkeyPatch,
                                            hub: _FakeCapabilityHub) -> None:
        _seed_state(hub, [
            _state_row("pipe-a", "A", "pending"),
            _state_row("pipe-b", "B", "pending"),
        ])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks",
                           query={"skip": "1", "limit": "1"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 2
        assert len(resp["payload"]["items"]) == 1

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
        import types

        import http_api

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
                                                       hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {}  # 方法缺失 → fake call KeyError
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/pause", "POST")
        assert resp["status"] == 404
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/resume", "POST")
        assert resp["status"] == 404

    async def test_cancel_cascade_rows_not_list(self, monkeypatch: pytest.MonkeyPatch,
                                                hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        hub._responses["pipeline-state"] = {"list": {"not": "a list"}}
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/pipe-x/cancel", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True
        assert resp["payload"]["cascaded_subtasks"] == 0

    async def test_cancel_cascade_capability_missing(self, monkeypatch: pytest.MonkeyPatch,
                                                     hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        # 无 pipeline-state 响应 → 级联异常 → 0，不阻断响应
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/pipe-x/cancel", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True

    async def test_cancel_task_missing_fallback_shape(self, monkeypatch: pytest.MonkeyPatch,
                                                      hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/no-such/cancel", "POST")
        assert resp["status"] == 200
        payload = resp["payload"]
        assert payload["cancelled"] is True
        assert payload["message"] == "任务已取消"
        assert payload["cascaded_subtasks"] == 0

    async def test_submit_task_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                           hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/no-such/submit", "POST")
        assert resp["status"] == 404

    async def test_evaluate_wrong_method_404(self, monkeypatch: pytest.MonkeyPatch,
                                             hub: _FakeCapabilityHub) -> None:
        """只有 POST /{task_id}/evaluate 映射为退役 410；其余方法仍走通用 no-route 404。"""
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/no-such/evaluate", "GET")
        assert resp["status"] == 404

    # ── 创建/提交失败路径 ──

    async def test_create_task_dispatch_failed_500(self, monkeypatch: pytest.MonkeyPatch,
                                                   hub: _FakeCapabilityHub) -> None:
        """工具派发失败信封（管道未出生）→ 500 fail-closed。"""
        hub._responses["tool-executor"] = {"invoke": {
            "success": False, "error": "执行管道未能创建（chat capability 未注入）",
            "error_code": "DISPATCH_FAILED",
        }}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "x", "description": "d", "agent_id": "main"})
        assert resp["status"] == 500
        assert "未能创建" in resp["payload"]["detail"]

    async def test_submit_task_dispatch_empty_queued(self, monkeypatch: pytest.MonkeyPatch,
                                                     hub: _FakeCapabilityHub) -> None:
        """注入派发响应缺 pipeline_id → 提交未生效，响应 queued 如实表达。"""
        _seed_state(hub, [_state_row("pipe-sub-1", "注入失败", "pending")])
        hub._responses["chat"] = {"send_message": {}}
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/tasks/pipe-sub-1/submit", "POST")
        assert resp["status"] == 200
        assert resp["payload"]["status"] == "queued"

    async def test_create_root_dispatch_failed_500(self, monkeypatch: pytest.MonkeyPatch,
                                                   hub: _FakeCapabilityHub) -> None:
        hub._responses["tool-executor"] = {"invoke": {
            "success": False, "error": "执行管道未能创建", "error_code": "DISPATCH_FAILED",
        }}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根", "description": "d", "thread_id": "th-1",
                                 "target_id": "main"})
        assert resp["status"] == 500

    # ── 服务不可用/能力缺失降级 ──

    async def test_service_unavailable_degradations(self, monkeypatch: pytest.MonkeyPatch,
                                                    hub: _FakeCapabilityHub) -> None:
        import http_api

        monkeypatch.setattr(http_api, "_capability", hub.get)
        monkeypatch.setattr(http_api, "get_project_registry", lambda: None)

        # 读面：state 不可用 → 200 空降级
        r = await http_api.handle_http("/ext/task_service/tasks", "GET", "", {}, None)
        assert json.loads(base64.b64decode(r["data"]["body"])) == {"items": [], "total": 0}
        r = await http_api.handle_http("/ext/task_service/tasks/debug/all", "GET", "", {}, None)
        assert json.loads(base64.b64decode(r["data"]["body"])) == {"items": [], "total": 0}

        # 写面：tool-executor 能力缺失 → 503（fail-honest，不空降级）
        def _missing(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(http_api, "_capability", _missing)
        r = await http_api.handle_http("/ext/task_service/tasks", "POST",
                                       json.dumps({"title": "x", "description": "d",
                                                   "agent_id": "main"}), {}, None)
        assert r["data"]["status"] == 503
        r = await http_api.handle_http("/ext/task_service/tasks/root", "POST",
                                       json.dumps({"title": "x", "description": "d",
                                                   "thread_id": "th", "target_id": "m"}),
                                       {}, None)
        assert r["data"]["status"] == 503

        # 删除/取消不再依赖 task_service：删除走 state 存在性（缺失 404），
        # 取消恒 200（挂起管线自行表达无 run）
        monkeypatch.setattr(http_api, "_capability", hub.get)
        r = await http_api.handle_http("/ext/task_service/tasks/x", "DELETE", "", {}, None)
        assert r["data"]["status"] == 404
        hub._responses["pipeline-executor"] = {"suspend_pipeline": {"run_id": "r-1"}}
        r = await http_api.handle_http("/ext/task_service/tasks/x/cancel", "POST", "", {}, None)
        assert r["data"]["status"] == 200

        # projects 域：登记簿不可用 → 读写面均 503（fail-honest，不空降级）
        monkeypatch.setattr(http_api, "_capability", lambda name: (_ for _ in ()).throw(KeyError(name)))
        r = await http_api.handle_http("/ext/task_service/projects", "GET", "", {}, None)
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
                                                          hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/p-9", "PATCH",
                           body={"title": "ignored"})
        assert resp["status"] == 503

    async def test_update_task_404_no_state_row(self, monkeypatch: pytest.MonkeyPatch,
                                                hub: _FakeCapabilityHub) -> None:
        hub._responses["pipeline-state"] = {"list": []}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/p-9", "PATCH",
                           body={"title": "ignored"})
        assert resp["status"] == 404

    # ── phase 映射 default ──

    async def test_phase_status_stopped_default(self, monkeypatch: pytest.MonkeyPatch,
                                                hub: _FakeCapabilityHub) -> None:
        """状态不在映射表（如 stopped）→ 默认 prepare/pending。"""
        _seed_state(hub, [_state_row("pipe-stopped", "暂停态", "stopped")])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/pipe-stopped/phase")
        assert resp["payload"] == {"taskId": "pipe-stopped", "currentPhase": "prepare",
                                   "phaseStatus": "pending"}

    # ── projects 边界 ──

    async def test_list_projects_bad_limit_fallback(self, monkeypatch: pytest.MonkeyPatch,
                                                    hub: _FakeCapabilityHub,
                                                    registry: Any) -> None:
        import project_registry as projects_mod

        registry.save(projects_mod.ProjectModel(title="A"))
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects",
                           query={"limit": "abc"})
        assert resp["status"] == 200
        assert resp["payload"]["limit"] == 20
        assert resp["payload"]["total"] == 1

    async def test_list_projects_page_offset(self, monkeypatch: pytest.MonkeyPatch,
                                             hub: _FakeCapabilityHub,
                                             registry: Any) -> None:
        import project_registry as projects_mod

        registry.save(projects_mod.ProjectModel(title="P1", created_at="2026-01-01T00:00:00"))
        registry.save(projects_mod.ProjectModel(title="P2", created_at="2026-02-01T00:00:00"))
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects",
                           query={"page": "2", "limit": "1"})
        assert resp["payload"]["total"] == 2
        assert len(resp["payload"]["items"]) == 1

    async def test_toggle_auto_execute_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                                   hub: _FakeCapabilityHub,
                                                   registry: Any) -> None:
        resp = await _http(monkeypatch, hub,
                           "/ext/task_service/projects/no-such/auto-execute", "POST", body={})
        assert resp["status"] == 404

    async def test_delete_project_missing_404(self, monkeypatch: pytest.MonkeyPatch,
                                              hub: _FakeCapabilityHub,
                                              registry: Any) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects/no-such",
                           "DELETE")
        assert resp["status"] == 404

    # ── 分发兜底 ──

    async def test_ac_unknown_subroute_404(self, monkeypatch: pytest.MonkeyPatch,
                                           hub: _FakeCapabilityHub) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t-1/ac/unknown")
        assert resp["status"] == 404

    async def test_handle_unexpected_error_500(self, monkeypatch: pytest.MonkeyPatch,
                                               hub: _FakeCapabilityHub) -> None:
        import http_api

        _install(monkeypatch, hub)

        async def _boom(task_id: str, _user: Any = None) -> Any:
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
        import types

        import http_api

        out = http_api._task_to_response({
            "id": "x",
            "title": "t",
            "status": "pending",
            "metadata": {"agent_level": types.SimpleNamespace(value="L9")},
        })
        assert out.agent_level == "L9"

    async def test_submit_event_inject_mode_branches(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """注入模式（重跑）分支：带描述 kickoff / 响应缺 id / chat 缺失显式报错。"""
        import http_api
        from task_birth import TaskBirthError

        _install(monkeypatch, hub)
        hub._responses["chat"] = {"send_message": {"pipeline_id": "t-1"}}
        # 注入模式带描述
        pid = await http_api._submit_task_event(
            title="t", description="注入描述", task_id="t-1",
        )
        assert pid == "t-1"
        kickoff = hub.handles["chat"].calls[0][1]["message"]
        assert "注入描述" in kickoff
        # chat 响应缺 pipeline_id → 空串（提交未生效）；pop 缓存句柄换响应代际
        hub.handles.pop("chat", None)
        hub._responses["chat"] = {"send_message": {}}
        assert await http_api._submit_task_event(title="t", task_id="t-1") == ""
        # chat 能力缺失（_capability KeyError）→ TaskBirthError（不降级不吞）
        def _missing(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(http_api, "_capability", _missing)
        with pytest.raises(TaskBirthError):
            await http_api._submit_task_event(title="t", task_id="t-1")

    async def test_direct_handler_dict_body_compat(self, monkeypatch: pytest.MonkeyPatch,
                                                   hub: _FakeCapabilityHub) -> None:
        """dict 直调兼容：SDK 按 dict 透传 body 时 handler 自行实例化模型。"""
        import http_api

        _install(monkeypatch, hub)
        hub._responses["tool-executor"] = {"invoke": _tool_ok("p-d1")}
        t = await http_api.create_task(
            {"title": "直调", "description": "d", "agent_id": "main"}, {}
        )
        assert t.id == "p-d1"
        r = await http_api.create_root_task(
            {"title": "直调根", "description": "d", "thread_id": "th", "target_id": "main"}, {},
        )
        assert r.id == "p-d1"
        _seed_state(hub, [_state_row("p-9", "G", "running")])
        u = await http_api.update_task("p-9", {"title": "ignored"}, {})
        assert u.id == "p-9"
        assert u.title == "G"

    async def test_handle_http_status_code_exception(self, monkeypatch: pytest.MonkeyPatch,
                                                     hub: _FakeCapabilityHub) -> None:
        """最终 except 的 status_code 分支（非 APIError 但带状态码的业务异常）。"""
        import http_api

        _install(monkeypatch, hub)

        class _HttpErr(Exception):
            status_code = 404
            detail = "gone-ish"

        async def _boom(task_id: str, _user: Any = None) -> Any:
            raise _HttpErr()

        monkeypatch.setattr(http_api, "get_task", _boom)
        result = await http_api.handle_http("/ext/task_service/tasks/x", "GET", "", {}, None)
        assert result["data"]["status"] == 404
        payload = json.loads(base64.b64decode(result["data"]["body"]).decode("utf-8"))
        assert payload == {"detail": "gone-ish"}
