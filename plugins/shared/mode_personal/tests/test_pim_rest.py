# @feature: FP-0.2.二 mode_personal 个人助理模式包 M0 | @ci: python-coverage
"""pim REST 路由单测 + 包契约测试（manifest 声明面 ↔ 路由核一致性）。

REST 面 = /ext/mode_personal/**（M0 清单：today/capture/三实体 CRUD/todo op/
undo/partner；focus 为 M2 接口位显式 501）。包契约：HOST_GUI 九裸名回避、
default 声明、工具声明带 output_schema+render、声明端点与路由核零漂移。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parents[1]
_MODES = _PKG.parents[1]

# HOST_GUI 九裸名（tool_schema 剥离集，撞名会在边界上下文被静默剥）
HOST_GUI_BARE_NAMES = {
    "App", "Click", "Type", "Snapshot", "Screenshot", "Scroll", "Move", "Shortcut", "Wait",
}


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pim_store = _load("pim_store", "mode_personal_test_rest_pim_store")
pim_rest = _load("pim_rest", "mode_personal_test_pim_rest")


@pytest.fixture
def store(tmp_path: Path) -> Any:
    return pim_store.PimStore(tmp_path / "data", clock=lambda: datetime(2026, 10, 12, 9, 0, 0))


class TestRestRoutes:
    def test_today(self, store: Any) -> None:
        status, payload = pim_rest.route(store, "/ext/mode_personal/today", "GET")
        assert status == 200
        assert payload["date"] == "2026-10-12"

    def test_capture_creates_and_low_confidence_422(self, store: Any) -> None:
        status, payload = pim_rest.route(
            store, "/ext/mode_personal/capture", "POST", None, {"text": "@明天 #工作 交日报"}
        )
        assert status == 201
        assert payload["todo"]["due"] == "2026-10-13"
        assert payload["op_id"]
        status, payload = pim_rest.route(
            store, "/ext/mode_personal/capture", "POST", None, {"text": "#工作 @每天"}
        )
        assert status == 422
        status, payload = pim_rest.route(
            store, "/ext/mode_personal/capture", "POST", None, {}
        )
        assert status == 400

    def test_todo_crud_full_cycle(self, store: Any) -> None:
        status, payload = pim_rest.route(
            store, "/ext/mode_personal/todos", "POST", None,
            {"title": "A", "domain": "工作", "due": "2026-10-12"},
        )
        assert status == 201
        todo_id = payload["record"]["id"]
        status, payload = pim_rest.route(store, "/ext/mode_personal/todos", "GET")
        assert status == 200 and payload["total"] == 1
        status, payload = pim_rest.route(
            store, "/ext/mode_personal/todos", "GET", {"domain": "生活"}
        )
        assert payload["total"] == 0  # domain 过滤
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}", "PATCH", None, {"progress": 30}
        )
        assert status == 200 and payload["record"]["progress"] == 30
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}", "PATCH", None, {}
        )
        assert status == 400  # 空 patch
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}", "PATCH", None, {"status": "bogus"}
        )
        assert status == 400  # 枚举校验
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}", "PATCH", None, {"progress": 1}
        )
        assert status == 200

    def test_todo_state_ops(self, store: Any) -> None:
        _, created = pim_rest.route(
            store, "/ext/mode_personal/todos", "POST", None, {"title": "A"}
        )
        todo_id = created["record"]["id"]
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}/complete", "POST"
        )
        assert status == 200
        assert payload["todo"]["status"] == "done"
        assert payload["partner"]["xp"] == 10
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}/defer", "POST", None, {"new_due": "2026-11-01"}
        )
        assert payload["todo"]["due"] == "2026-11-01"
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/todos/{todo_id}/split", "POST", None, {"parts": ["a"]}
        )
        assert status == 200 and len(payload["created"]) == 1
        status, _ = pim_rest.route(
            store, f"/ext/mode_personal/todos/missing/complete", "POST"
        )
        assert status == 404

    def test_events_and_habits_crud(self, store: Any) -> None:
        for coll, fields in [
            ("events", {"title": "例会", "at": "2026-10-12 10:00"}),
            ("habits", {"title": "跑步", "schedule": "每天"}),
        ]:
            status, payload = pim_rest.route(store, f"/ext/mode_personal/{coll}", "POST", None, fields)
            assert status == 201, (coll, payload)
            record_id = payload["record"]["id"]
            status, payload = pim_rest.route(store, f"/ext/mode_personal/{coll}", "GET")
            assert payload["total"] == 1
            status, payload = pim_rest.route(
                store, f"/ext/mode_personal/{coll}/{record_id}", "PATCH", None,
                {"title": fields["title"] + "改"} if coll == "habits" else {"at": "2026-10-12 11:00"},
            )
            assert status == 200
        # event 缺 at → 400
        status, payload = pim_rest.route(
            store, "/ext/mode_personal/events", "POST", None, {"title": "坏事件"}
        )
        assert status == 400

    def test_habit_checkin_roundtrip(self, store: Any) -> None:
        """习惯打卡：history 追加当天 + op 可撤销；同日重复打卡幂等（op_id=None）。"""
        _, created = pim_rest.route(
            store, "/ext/mode_personal/habits", "POST", None, {"title": "跑步", "schedule": "每天"}
        )
        habit_id = created["record"]["id"]
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/habits/{habit_id}/checkin", "POST"
        )
        assert status == 200
        assert payload["habit"]["history"] == ["2026-10-12"]
        assert payload["op_id"]
        # 幂等：同日重复打卡零变化（op_id=None，回执照常 200）
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/habits/{habit_id}/checkin", "POST"
        )
        assert status == 200 and payload["op_id"] is None
        assert payload["habit"]["history"] == ["2026-10-12"]
        # 撤销打卡 → history 回退（同一 op 可逆）
        _, today = pim_rest.route(store, "/ext/mode_personal/today", "GET")
        op_id = today["op_log"][-1]["op_id"]
        status, _ = pim_rest.route(store, f"/ext/mode_personal/undo/{op_id}", "POST")
        assert status == 200
        _, listed = pim_rest.route(store, "/ext/mode_personal/habits", "GET")
        # undo 恢复打卡前原状：创建时未带 history 字段 → 回退后字段缺席
        assert listed["items"][0].get("history") in (None, [])
        # 不存在 → 404
        status, _ = pim_rest.route(store, "/ext/mode_personal/habits/nope/checkin", "POST")
        assert status == 404

    def test_undo_and_partner(self, store: Any) -> None:
        _, created = pim_rest.route(
            store, "/ext/mode_personal/todos", "POST", None, {"title": "A"}
        )
        todo_id = created["record"]["id"]
        pim_rest.route(store, f"/ext/mode_personal/todos/{todo_id}/complete", "POST")
        status, payload = pim_rest.route(store, "/ext/mode_personal/partner", "GET")
        assert payload["partner"]["xp"] == 10
        # undo 借 today 聚合 op 尾定位（前端撤销条同款取数）
        _, today = pim_rest.route(store, "/ext/mode_personal/today", "GET")
        op_id = today["op_log"][-1]["op_id"]
        status, payload = pim_rest.route(store, f"/ext/mode_personal/undo/{op_id}", "POST")
        assert status == 200 and payload["undo_of"] == op_id
        _, partner = pim_rest.route(store, "/ext/mode_personal/partner", "GET")
        assert partner["partner"]["xp"] == 0
        status, _ = pim_rest.route(store, "/ext/mode_personal/undo/nope", "POST")
        assert status == 404

    def test_focus_start_stop_roundtrip(self, store: Any) -> None:
        """番茄启停同轨：start=doing+标记；stop=清标记；错误翻 404/400。"""
        _, created = pim_rest.route(
            store, "/ext/mode_personal/todos", "POST", None, {"title": "专注"}
        )
        todo_id = created["record"]["id"]
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/focus/{todo_id}/start", "POST"
        )
        assert status == 200
        assert payload["todo"]["status"] == "doing"
        assert payload["todo"]["focus_started_at"] == "2026-10-12T09:00:00"
        assert payload["op_id"]
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/focus/{todo_id}/stop", "POST"
        )
        assert status == 200
        assert payload["todo"]["focus_started_at"] is None
        status, payload = pim_rest.route(
            store, f"/ext/mode_personal/focus/{todo_id}/stop", "POST"
        )
        assert status == 400  # 无进行中的番茄
        status, _ = pim_rest.route(store, "/ext/mode_personal/focus/missing/start", "POST")
        assert status == 404

    def test_unknown_route_404(self, store: Any) -> None:
        status, payload = pim_rest.route(store, "/ext/mode_personal/nope", "GET")
        assert status == 404


class TestPackageContract:
    """包契约（装载面真实文件，不 mock）：manifest/声明面/路由一致性。"""

    @pytest.fixture(scope="class")
    def manifest(self) -> dict[str, Any]:
        with open(_PKG / "plugin.json", encoding="utf-8") as fh:
            return json.load(fh)

    @pytest.fixture(scope="class")
    def mode_yaml(self) -> dict[str, Any]:
        import yaml

        return yaml.safe_load((_PKG / "mode.yaml").read_text(encoding="utf-8"))

    def test_tool_declaration_contract(self, manifest: dict[str, Any]) -> None:
        tools = manifest["capabilities"]["tools"]
        assert [t["name"] for t in tools] == ["pim"]  # 单工具
        tool = tools[0]
        assert tool["input_schema"]["properties"]["action"]["enum"] == [
            "capture", "query", "op",
        ]
        # tool_core fail-closed 契约：output_schema + render 必带
        assert isinstance(tool["output_schema"], dict)
        assert tool["output_schema"]["required"]
        assert isinstance(tool["render"], dict) and tool["render"].get("card")
        # render.open_page 声明（工具卡「打开今日总览」跳转意图，声明驱动）
        assert tool["render"]["open_page"] == {"page": "pim_today", "label": "打开今日总览"}
        # 命名避开 HOST_GUI 九裸名（撞名在边界上下文被静默剥）
        assert tool["name"] not in HOST_GUI_BARE_NAMES
        assert manifest["id"] == "mode_personal"
        assert manifest["plugin_type"] == "system"
        assert manifest["host_type"] == "sidecar"
        assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])

    def test_service_and_http_declarations(self, manifest: dict[str, Any]) -> None:
        services = {s["name"] for s in manifest["capabilities"]["services"]}
        assert "pim.query" in services
        assert "http.handle" in services
        endpoints = manifest["http_endpoints"]
        paths = {(e["method"], e["path"]) for e in endpoints}
        # M0 REST 清单（任务卡口径）逐项在册
        for method, path in [
            ("GET", "/ext/mode_personal/today"),
            ("POST", "/ext/mode_personal/capture"),
            ("GET", "/ext/mode_personal/todos"),
            ("POST", "/ext/mode_personal/todos"),
            ("PATCH", "/ext/mode_personal/todos/{id}"),
            ("GET", "/ext/mode_personal/events"),
            ("POST", "/ext/mode_personal/events"),
            ("PATCH", "/ext/mode_personal/events/{id}"),
            ("GET", "/ext/mode_personal/habits"),
            ("POST", "/ext/mode_personal/habits"),
            ("PATCH", "/ext/mode_personal/habits/{id}"),
            ("POST", "/ext/mode_personal/todos/{id}/complete"),
            ("POST", "/ext/mode_personal/todos/{id}/defer"),
            ("POST", "/ext/mode_personal/todos/{id}/split"),
            ("POST", "/ext/mode_personal/undo/{op_id}"),
            ("GET", "/ext/mode_personal/partner"),
            ("POST", "/ext/mode_personal/focus/{id}/start"),
            ("POST", "/ext/mode_personal/focus/{id}/stop"),
            ("POST", "/ext/mode_personal/habits/{id}/checkin"),
            ("GET", "/ext/mode_personal/page/pim-today"),
        ]:
            assert (method, path) in paths, (method, path)
        for e in endpoints:
            assert e["auth"] == "user"
            assert e["handler_capability"] == "http.handle"
            assert e["path"].startswith("/ext/mode_personal/")

    def test_declared_endpoints_reachable_via_route_core(self, manifest: dict[str, Any], store: Any) -> None:
        """声明端点与路由核零漂移：字面端点直打路由核不得 404（模板段实例化）。

        page/ 前缀端点（webview HTML 面）由 server 层 html_file_response 特判，
        不经路由核——从本检查排除（mode_roleplay 面板页同形态）。
        """
        literal_paths = [
            e["path"] for e in manifest["http_endpoints"]
            if "{" not in e["path"] and "/page/" not in e["path"]
        ]
        methods = {e["path"]: e["method"] for e in manifest["http_endpoints"] if "{" not in e["path"]}
        for path in literal_paths:
            status, _ = pim_rest.route(store, path, methods[path])
            assert status != 404, path

    def test_page_declaration_and_html_contract(self, manifest: dict[str, Any]) -> None:
        """pim_today 页面契约：contributes.pages 声明 + 包内 HTML 存在与关键结构。"""
        pages = manifest["contributes"]["pages"]
        assert [p["id"] for p in pages] == ["pim_today"]
        page = pages[0]
        assert page["widget"] == "webview"
        assert page["props"]["pluginId"] == "mode_personal"
        assert page["props"]["htmlPath"] == "/page/pim-today"
        assert page["mode"] == "personal"
        assert page["space"] == "workspace" and page["slot"] == "tab"
        html_path = _PKG / "webview" / "pim_today.html"
        assert html_path.is_file(), "webview/pim_today.html 缺件（端点将 500）"
        html = html_path.read_text(encoding="utf-8")
        # 两视图层 + 桥接协议 + 定位协议 + 素材内嵌（自包含，零外部请求）
        for token in [
            "wb-cols", "game-panel", "gp-tabs",
            "theme.sync", "agentosFetch", "widget.event", "locate",
            "data:image/svg+xml;base64,",
        ]:
            assert token in html, token
        assert "http://" not in html and "https://" not in html, "页面须零外部请求（自包含）"

    def test_mode_yaml_declaration(self, mode_yaml: dict[str, Any]) -> None:
        assert mode_yaml["mode"] == "personal"
        assert mode_yaml["default"] is True
        assert mode_yaml["persona"]["replace"] is True
        assert mode_yaml["persona"]["from"]
        assert mode_yaml["chain"]["entry"] == "main"
        assert set(mode_yaml["chain"]) <= {"entry", "expected_path", "entry_override"}
        # 呈报契约概要在 description（模式说明直取面，零 material.py）
        assert "呈报契约" in (mode_yaml["description"] or "")
        # 不新增管道（缺省共享 autonomous）
        assert "pipelines" not in mode_yaml
        assert not (_PKG / "material.py").exists()
