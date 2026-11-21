"""审批非阻塞 M1 测试（ticket 写入 / mode=async / approval.fold / inbox 端点）。

按设计文档 docs/working/3a9cc256c6d7_审批非阻塞方案设计.md §5.7/§5.10/§5.11：
- ticket 落 pipeline_state（approval.tickets.<id> + approval.inbox_rev），经
  注入的 state-writer 回调走 pipeline-state.update 单一路径（审计铁律）；
- approval.fold 聚合注入 + 两阶段清理（consumed_at 先置位 → delete_keys 真删）；
- inbox 端点只返回未决 + 短暂已决窗口（consumed_at 置位后即从输出消失）；
- mode=async 非阻塞：立即返回 submitted，不建等待事件。

测试经 importlib 显式加载模块（簇 J 范式），fake 桩替代外部依赖
（pipeline-state capability / notifier / human-interaction capability），
断言可观察行为（调用参数/返回值/副作用），不断言内部实现细节。
"""

# @feature: FP-0.2.〇 管道引擎 | @vision: V2 全能闭环 | @ci: plugins-coverage
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HUMAN_DIR = _REPO_ROOT / "plugins" / "shared" / "human"
_APPROVAL_DIR = _REPO_ROOT / "plugins" / "shared" / "approval"
_SECURITY_DIR = _REPO_ROOT / "plugins" / "shared" / "security_check"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"

for _p in (_HUMAN_DIR, _APPROVAL_DIR, _SECURITY_DIR, _SHARED_ROOT, _SDK_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(module_path: Path, name: str) -> Any:
    """按显式路径加载模块（唯一名，模块级状态互不污染）。"""
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeStateCap:
    """假 pipeline-state capability：list 返回预置行，update 记录调用。

    delete 真删语义模拟：记录 delete_keys 并从预置行中移除对应键
    （内核 handle_pipeline_state_update 的可观察行为镜像）。
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None, fail_delete: bool = False):
        self.rows = rows or []
        self.updates: list[dict[str, Any]] = []
        self.fail_delete = fail_delete

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "list":
            return self.rows
        if method == "update":
            self.updates.append(params)
            pid = params.get("pipeline_id", "")
            fields = params.get("fields") or {}
            delete_keys = params.get("delete_keys") or []
            if self.fail_delete and delete_keys:
                raise RuntimeError("db busy (fake)")
            row = next((r for r in self.rows if r.get("pipeline_id") == pid), None)
            if row is not None:
                for k, v in fields.items():
                    row[k] = v
                for k in delete_keys:
                    row.pop(k, None)
            return {"ok": True, "deleted": delete_keys}
        raise ValueError(f"unexpected method: {method}")

    def written_fields(self) -> list[dict[str, Any]]:
        return [u.get("fields") or {} for u in self.updates]


def _make_ticket_row(
    pipeline_id: str, tickets: dict[str, dict[str, Any]], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """构造 pipeline-state.list 聚合行（扁平点号键形态，同 summarize_state 出口）。"""
    row: dict[str, Any] = {"pipeline_id": pipeline_id, "thread_id": pipeline_id, "source": "memory"}
    for k, v in tickets.items():
        row[f"approval.tickets.{k}"] = v
    if extra:
        row.update(extra)
    return row


def _pending_ticket(tid: str, title: str = "合并分支", **over: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "ticket_id": tid,
        "category": "decision",
        "blocking": False,
        "title": title,
        "status": "pending",
        "pipeline_id": "p1",
        "decision": None,
        "consumed_at": None,
    }
    t.update(over)
    return t


# ══ human service/server：ticket 写入 ════════════════════════════════


class TestTicketWrite:
    """create_choice_request 写 ticket：键形态/字段/category 三类/blocking。"""

    @pytest.fixture
    def service(self) -> Any:
        mod = _load(_HUMAN_DIR / "service.py", "human_service_m1")
        svc = mod.HumanInteractionService()
        cap = FakeStateCap()
        svc.set_state_writer(lambda: None) if False else None

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            await cap.call("update", {"pipeline_id": pipeline_id, "fields": fields})

        svc.set_state_writer(_writer)
        svc._fake_cap = cap
        return svc

    async def test_ticket_written_on_create_choice(self, service: Any) -> None:
        """创建 choice 请求后 ticket 落 state：键 = approval.tickets.<id>，
        同批写 approval.inbox_rev（变更检测版本号）。"""
        rid = await service.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="批准合并",
            options=[{"id": "approve", "label": "批准"}],
            pipeline_id="p1", agent_name="L1 编排",
        )
        fields_list = service._fake_cap.written_fields()
        assert fields_list, "state_writer 未被调用"
        ticket = fields_list[0][f"approval.tickets.{rid}"]
        assert ticket["ticket_id"] == rid
        assert ticket["title"] == "批准合并"
        assert ticket["status"] == "pending"
        assert ticket["options"] == [{"id": "approve", "label": "批准"}]
        assert ticket["pipeline_id"] == "p1"
        assert "approval.inbox_rev" in fields_list[0]

    @pytest.mark.parametrize("category", ["decision", "permission"])
    async def test_category_explicit_pass_through(self, service: Any, category: str) -> None:
        """发起方显式传 category（decision/permission）→ ticket 如实携带，
        不由后端按标题猜测（设计 §5.8.2）。"""
        rid = await service.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "a", "label": "A"}], pipeline_id="p1", category=category,
        )
        ticket = service._fake_cap.written_fields()[0][f"approval.tickets.{rid}"]
        assert ticket["category"] == category

    async def test_category_default_generic(self, service: Any) -> None:
        """未传 category 缺省 generic（其余交互）。"""
        rid = await service.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "a", "label": "A"}], pipeline_id="p1",
        )
        ticket = service._fake_cap.written_fields()[0][f"approval.tickets.{rid}"]
        assert ticket["category"] == "generic"

    async def test_blocking_true_on_blocking_path(self, service: Any) -> None:
        """阻塞路径（调用方将 wait_for_choice）blocking=True。"""
        rid = await service.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "a", "label": "A"}], pipeline_id="p1",
        )
        ticket = service._fake_cap.written_fields()[0][f"approval.tickets.{rid}"]
        assert ticket["blocking"] is True

    async def test_state_writer_failure_does_not_block_create(self, service: Any) -> None:
        """state 写失败（DB 忙等）不阻断审批主链（非核心 best-effort：
        日志带上下文，request_id 照常返回）。"""

        async def _boom(pipeline_id: str, fields: dict[str, Any]) -> None:
            raise RuntimeError("db busy")

        service.set_state_writer(_boom)
        rid = await service.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "a", "label": "A"}], pipeline_id="p1",
        )
        assert rid, "主链仍返回 request_id"

    async def test_no_ticket_without_pipeline_id(self, service: Any) -> None:
        """无 pipeline_id（无归属管道）不写 ticket——无落点，主链不受影响。"""
        rid = await service.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="", title="t",
            options=[{"id": "a", "label": "A"}],
        )
        assert rid
        assert service._fake_cap.updates == []


# ══ human service/server：mode=async 非阻塞 ══════════════════════════


class TestAsyncMode:
    """mode=async：立即返回 submitted，不等待；阻塞路径行为不变。"""

    @pytest.fixture
    def server(self) -> Any:
        mod = _load(_HUMAN_DIR / "server.py", "human_server_m1")
        svc_mod = _load(_HUMAN_DIR / "service.py", "human_service_m1_async")
        svc = svc_mod.HumanInteractionService()

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            return None

        svc.set_state_writer(_writer)
        mod._service = svc
        return mod

    async def test_async_returns_submitted_immediately(self, server: Any) -> None:
        """mode=async：立即返回 status=submitted + ticket_id，不等待用户。

        性质断言：返回耗时远小于 timeout（1s 内完成，timeout 3600s 未触发）。
        """
        import time

        started = time.monotonic()
        result = await server.human_interaction(
            title="合并分支", description="d",
            options=[{"id": "approve", "label": "批准"}],
            pipeline_id="p1", session_id="p1",
            timeout_seconds=3600, mode="async",
        )
        elapsed = time.monotonic() - started
        assert result["status"] == "submitted"
        assert result.get("ticket_id"), "返回含 ticket_id"
        assert elapsed < 1.0, f"非阻塞应立即返回，实测 {elapsed:.2f}s"

    async def test_async_no_pending_event(self, server: Any) -> None:
        """async 模式不建等待事件（_pending_events 无该 rid）——工具不占等待位。"""
        result = await server.human_interaction(
            title="t", options=[{"id": "a", "label": "A"}],
            pipeline_id="p1", session_id="p1", mode="async",
        )
        rid = result["ticket_id"]
        assert rid not in server._service._pending_events

    async def test_blocking_mode_still_waits(self, server: Any) -> None:
        """缺省（阻塞）路径行为不变：等待用户作答后返回 completed。"""
        task = asyncio.ensure_future(server.human_interaction(
            title="t", options=[{"id": "a", "label": "A"}],
            pipeline_id="p1", session_id="p1", timeout_seconds=30,
        ))
        await asyncio.sleep(0.05)
        records = list(server._service._requests.values())
        assert records, "阻塞路径创建请求"
        await server._service.submit_response(
            records[0]["id"], response_type="answered", selected_option="a",
        )
        result = await asyncio.wait_for(task, timeout=5)
        assert result["status"] == "completed"
        assert result["selected_option"] == "a"

    async def test_async_ticket_blocking_false(self, server: Any) -> None:
        """async 路径的 ticket blocking=False（非阻塞）。"""
        cap = FakeStateCap()
        svc = server._service

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            await cap.call("update", {"pipeline_id": pipeline_id, "fields": fields})

        svc.set_state_writer(_writer)
        result = await server.human_interaction(
            title="t", options=[{"id": "a", "label": "A"}],
            pipeline_id="p1", session_id="p1", mode="async",
        )
        ticket = cap.written_fields()[0][f"approval.tickets.{result['ticket_id']}"]
        assert ticket["blocking"] is False


# ══ human service：submit_response 回写 ticket（T1 决定面）════════════


class TestDecisionWriteback:
    """决定发生时回写 ticket 状态（不删键——两阶段第一段）。"""

    async def _make_service(self) -> tuple[Any, FakeStateCap]:
        mod = _load(_HUMAN_DIR / "service.py", "human_service_m1_dec")
        svc = mod.HumanInteractionService()
        cap = FakeStateCap()
        state: dict[str, Any] = {}

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            await cap.call("update", {"pipeline_id": pipeline_id, "fields": fields})
            for k, v in fields.items():
                if k.startswith("approval.tickets.") and isinstance(v, dict):
                    state[k] = v

        svc.set_state_writer(_writer)
        rid = await svc.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "approve", "label": "批准"}], pipeline_id="p1",
        )
        return svc, cap, rid, state

    async def test_submit_response_updates_ticket_status(self) -> None:
        """submit_response 后 ticket status 变已决（approved/denied），
        键仍在 state（fold 下轮可读）——G4 第一段。"""
        mod = _load(_HUMAN_DIR / "service.py", "human_service_m1_dec2")
        svc = mod.HumanInteractionService()
        updates: list[tuple[str, dict[str, Any]]] = []

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            updates.append((pipeline_id, fields))

        svc.set_state_writer(_writer)
        rid = await svc.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "approve", "label": "批准"}], pipeline_id="p1",
        )
        updates.clear()
        await svc.submit_response(rid, response_type="answered", selected_option="approve")
        assert updates, "决定面写 ticket"
        fields = updates[-1][1]
        ticket = fields[f"approval.tickets.{rid}"]
        assert ticket["status"] == "approved"
        assert ticket["decision"] is not None
        # 键仍在：回写不删（fold 注入后才删）
        assert "delete_keys" not in str(fields)

    async def test_deny_writes_denied_status(self) -> None:
        """拒绝路径 ticket status=denied（含 decision 详情）。"""
        mod = _load(_HUMAN_DIR / "service.py", "human_service_m1_dec3")
        svc = mod.HumanInteractionService()
        last_fields: dict[str, Any] = {}

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            last_fields.clear()
            last_fields.update(fields)

        svc.set_state_writer(_writer)
        rid = await svc.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "deny", "label": "拒绝"}], pipeline_id="p1",
        )
        await svc.submit_response(rid, response_type="denied")
        ticket = last_fields[f"approval.tickets.{rid}"]
        assert ticket["status"] == "denied"

    async def test_timeout_writes_expired_never_approved(self) -> None:
        """超时回写 expired——绝不出现 timeout→approved 分支（G5）。"""
        mod = _load(_HUMAN_DIR / "service.py", "human_service_m1_dec4")
        svc = mod.HumanInteractionService()
        last_fields: dict[str, Any] = {}

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            last_fields.clear()
            last_fields.update(fields)

        svc.set_state_writer(_writer)
        rid = await svc.create_choice_request(
            session_id="s1", thread_id="s1", tab_id="p1", title="t",
            options=[{"id": "a", "label": "A"}], pipeline_id="p1",
            timeout_seconds=1,
        )
        await asyncio.sleep(1.3)
        ticket = last_fields.get(f"approval.tickets.{rid}") or {}
        assert ticket.get("status") == "expired"


# ══ approval.fold 聚合注入步（server.py）══════════════════════════════


class TestApprovalFold:
    """approval.fold：聚合注入 + 两阶段清理 + 幂等不重复注入。"""

    def _load_server(self) -> Any:
        return _load(_HUMAN_DIR / "server.py", "human_server_m1_fold")

    def _fold_cap(self, rows: list[dict[str, Any]], fail_delete: bool = False) -> FakeStateCap:
        return FakeStateCap(rows=rows, fail_delete=fail_delete)

    async def _run_fold(self, server: Any, cap: FakeStateCap) -> dict[str, Any]:
        async def _make_cap(name: str) -> Any:
            return cap

        # fold 步内经 plugin.get_capability("pipeline-state") 取真值——
        # 测试用 fake plugin 替换模块级 plugin 单例的能力面。
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        return await server.approval_fold({"pipeline_id": "p1"}, None)

    async def test_aggregates_pending_tickets_into_dynamic_vars(self) -> None:
        """多审批项聚合：同管道多条 pending 折叠注入单条消息尾部。"""
        server = self._load_server()
        rows = [_make_ticket_row("p1", {
            "ap-1": _pending_ticket("ap-1", "合并 12 个文件"),
            "ap-2": _pending_ticket("ap-2", "删除 build 目录", category="permission", blocking=True),
        })]
        cap = self._fold_cap(rows)
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "本轮上下文"}}
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        result = await server.approval_fold(state, None)
        dvm = result["state_updates"]["prompt.dynamic_vars"]
        assert "合并 12 个文件" in dvm["content"]
        assert "删除 build 目录" in dvm["content"]
        assert dvm["content"].startswith("本轮上下文"), "尾部追加，不覆盖原内容"

    async def test_includes_decision_results_in_injection(self) -> None:
        """已决未消费条目注入决定结果（G3：决定后 agent 下一轮能看到）。"""
        server = self._load_server()
        decided = _pending_ticket("ap-9", "推送 release")
        decided["status"] = "approved"
        decided["decision"] = {"response_type": "answered", "selected_option": "approve"}
        rows = [_make_ticket_row("p1", {"ap-9": decided})]
        cap = self._fold_cap(rows)
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        result = await server.approval_fold(state, None)
        content = result["state_updates"]["prompt.dynamic_vars"]["content"]
        assert "推送 release" in content
        assert "批准" in content or "approved" in content

    async def test_two_phase_cleanup_marks_consumed_then_deletes(self) -> None:
        """两阶段清理：先写 consumed_at（幂等闸）→ delete_keys 真删。"""
        server = self._load_server()
        decided = _pending_ticket("ap-9", "推送 release")
        decided["status"] = "denied"
        decided["decision"] = {"response_type": "answered", "selected_option": "deny"}
        rows = [_make_ticket_row("p1", {"ap-9": decided})]
        cap = self._fold_cap(rows)
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        await server.approval_fold(state, None)
        upd = [u for u in cap.updates if u["pipeline_id"] == "p1"]
        assert upd, "fold 产生 state 写"
        # 第一段：consumed_at 置位经 update fields（幂等先置位）
        marked = [u for u in upd if any(
            k.endswith("consumed_at") for k in (u.get("fields") or {})
        )]
        assert marked, "consumed_at 先置位"
        # 第二段：真删经 delete_keys
        deleters = [u for u in upd if u.get("delete_keys")]
        assert deleters, "delete_keys 真删"
        assert any("approval.tickets.ap-9" in u["delete_keys"] for u in deleters)
        # fake 镜像内核行为：删除后行内键已消失
        assert "approval.tickets.ap-9" not in rows[0]

    async def test_consumed_ticket_not_reinjected(self) -> None:
        """已消费（consumed_at 非空）不再注入（G7 幂等：fold 崩溃重试不重复打扰）。"""
        server = self._load_server()
        consumed = _pending_ticket("ap-7", "旧决定")
        consumed["status"] = "approved"
        consumed["consumed_at"] = "2026-11-20T10:00:00+08:00"
        rows = [_make_ticket_row("p1", {"ap-7": consumed})]
        cap = self._fold_cap(rows)
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        result = await server.approval_fold(state, None)
        content = result["state_updates"]["prompt.dynamic_vars"]["content"]
        assert "旧决定" not in content
        # 只重试删除、不重复注入
        deleters = [u for u in cap.updates if u.get("delete_keys")]
        assert any("approval.tickets.ap-7" in u["delete_keys"] for u in deleters)

    async def test_delete_failure_only_retries_no_reinject(self) -> None:
        """删除失败：consumed_at 已置位 → 下轮不重复注入，只重试删除（G7）。"""
        server = self._load_server()
        decided = _pending_ticket("ap-8", "失败重试")
        decided["status"] = "approved"
        decided["decision"] = {"response_type": "answered"}
        rows = [_make_ticket_row("p1", {"ap-8": decided})]
        cap = self._fold_cap(rows, fail_delete=True)
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        # 第一轮：注入 + consumed_at 置位成功 + 删除失败
        result = await server.approval_fold(state, None)
        assert "失败重试" in result["state_updates"]["prompt.dynamic_vars"]["content"]
        # 第二轮（删除恢复）：consumed_at 已置位 → 不重复注入，仅补删
        cap.fail_delete = False
        cap.updates.clear()
        result2 = await server.approval_fold(state, None)
        content = result2["state_updates"]["prompt.dynamic_vars"]["content"]
        assert "失败重试" not in content, "已消费条目不得重复注入（G7）"
        deleters = [u for u in cap.updates if u.get("delete_keys")]
        assert any("approval.tickets.ap-8" in u["delete_keys"] for u in deleters), "重试删除"

    async def test_no_dynamic_vars_zero_action(self) -> None:
        """prompt.dynamic_vars 缺失：零动作直通（todo_fold 同款降级契约）。"""
        server = self._load_server()
        rows = [_make_ticket_row("p1", {"ap-1": _pending_ticket("ap-1")})]
        cap = self._fold_cap(rows)
        result = await self._run_fold_via_state(server, cap, {"pipeline_id": "p1"})
        assert result["state_updates"] == {}

    async def _run_fold_via_state(
        self, server: Any, cap: FakeStateCap, state: dict[str, Any]
    ) -> dict[str, Any]:
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        return await server.approval_fold(state, None)

    async def test_no_tickets_zero_writes(self) -> None:
        """无 ticket：零写入（无 approval.* 键的管道 fold 不产生任何 state 变更）。"""
        server = self._load_server()
        cap = self._fold_cap([{"pipeline_id": "p1", "thread_id": "p1", "source": "memory"}])
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        result = await self._run_fold_via_state(server, cap, state)
        assert result["state_updates"] == {}

    async def test_capability_missing_fails_soft(self) -> None:
        """pipeline-state 能力未注入：warning 直通零写入（不阻断管道）。"""
        server = self._load_server()
        server.plugin._capabilities.pop("pipeline-state", None)  # type: ignore[attr-defined]
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        result = await server.approval_fold(state, None)
        assert result["state_updates"] == {}

    async def test_cross_pipeline_aggregation(self) -> None:
        """跨管道聚合：fold 只注入本管道的 ticket（state.list 全租户行 →
        按 state.pipeline_id 过滤），他管道的不串台。"""
        server = self._load_server()
        rows = [
            _make_ticket_row("p1", {"ap-1": _pending_ticket("ap-1", "本管道的事")}),
            _make_ticket_row("p2", {"ap-2": _pending_ticket("ap-2", "别管道的事")}),
        ]
        cap = self._fold_cap(rows)
        state = {"pipeline_id": "p1", "prompt.dynamic_vars": {"content": "base"}}
        result = await self._run_fold_via_state(server, cap, state)
        content = result["state_updates"]["prompt.dynamic_vars"]["content"]
        assert "本管道的事" in content
        assert "别管道的事" not in content


# ══ inbox 只读端点（approval/server.py http.handle 分发）══════════════


def _decode_http(data: dict[str, Any]) -> Any:
    """解开 HttpHandleResponse（body base64 JSON）→ dict。"""
    import base64
    import json

    assert isinstance(data, dict)
    assert "body" in data
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


class TestInboxEndpoint:
    """GET /ext/approval_service/approval/inbox：聚合读面。"""

    def _load_server(self) -> Any:
        return _load(_APPROVAL_DIR / "server.py", "approval_server_m1")

    async def _handle(self, server: Any, cap: FakeStateCap, query: dict[str, str]) -> Any:
        server.plugin._capabilities["pipeline-state"] = cap  # type: ignore[attr-defined]
        res = await server.http_handle(
            path="/ext/approval_service/approval/inbox",
            method="GET",
            query=query,
        )
        assert res.get("success") is True
        return _decode_http(res["data"])

    async def test_inbox_returns_pending_grouped_by_pipeline(self) -> None:
        """聚合形态：按管道分组的未决 ticket（未决优先 + category 字段透出）。"""
        server = self._load_server()
        rows = [
            _make_ticket_row("p1", {
                "ap-1": _pending_ticket("ap-1", "合并 12 个文件"),
                "ap-2": _pending_ticket("ap-2", "删除 build", category="permission", blocking=True),
            }),
            _make_ticket_row("p2", {"ap-3": _pending_ticket("ap-3", "发布第 3 章")}),
        ]
        cap = FakeStateCap(rows=rows)
        # 缺省（无 pipeline_id）= 全部管道各自成组（过滤语义由
        # test_inbox_filters_by_pipeline_id 单独锚定）；本测断言聚合形态
        payload = await self._handle(server, cap, {})
        assert payload["pipelines"], "返回管道分组"
        p1 = next(p for p in payload["pipelines"] if p["pipeline_id"] == "p1")
        titles = [t["title"] for t in p1["tickets"]]
        assert "合并 12 个文件" in titles
        assert "删除 build" in titles
        cats = {t["category"] for t in p1["tickets"]}
        assert cats == {"decision", "permission"}
        assert "ap-3" not in str(p1["tickets"]), "不带 pipeline_id 时不含他管道？——缺省全量，p2 应单独成组"
        p2 = next((p for p in payload["pipelines"] if p["pipeline_id"] == "p2"), None)
        assert p2 is not None, "缺省（无 pipeline_id）返回全部管道"

    async def test_inbox_filters_by_pipeline_id(self) -> None:
        """?pipeline_id= 只返回该管道。"""
        server = self._load_server()
        rows = [
            _make_ticket_row("p1", {"ap-1": _pending_ticket("ap-1")}),
            _make_ticket_row("p2", {"ap-2": _pending_ticket("ap-2")}),
        ]
        cap = FakeStateCap(rows=rows)
        payload = await self._handle(server, cap, {"pipeline_id": "p2"})
        assert [p["pipeline_id"] for p in payload["pipelines"]] == ["p2"]

    async def test_inbox_excludes_consumed(self) -> None:
        """已消费（consumed_at 非空）不进 inbox——state 无残留堆积的读面证据。"""
        server = self._load_server()
        consumed = _pending_ticket("ap-7", "旧决定")
        consumed["status"] = "approved"
        consumed["consumed_at"] = "2026-11-20T10:00:00+08:00"
        rows = [_make_ticket_row("p1", {
            "ap-7": consumed,
            "ap-1": _pending_ticket("ap-1", "还在等"),
        })]
        cap = FakeStateCap(rows=rows)
        payload = await self._handle(server, cap, {})
        p1 = next(p for p in payload["pipelines"] if p["pipeline_id"] == "p1")
        titles = [t["title"] for t in p1["tickets"]]
        assert "旧决定" not in titles
        assert "还在等" in titles

    async def test_inbox_includes_decided_unconsumed_window(self) -> None:
        """短暂已决窗口：已决未消费的条目仍在 inbox（决定后到 fold 注入前，
        fold 要读它；前端按 status 呈现）。"""
        server = self._load_server()
        decided = _pending_ticket("ap-9", "刚批完")
        decided["status"] = "approved"
        decided["decision"] = {"selected_option": "approve"}
        rows = [_make_ticket_row("p1", {"ap-9": decided})]
        cap = FakeStateCap(rows=rows)
        payload = await self._handle(server, cap, {})
        p1 = payload["pipelines"][0]
        assert p1["tickets"][0]["status"] == "approved"

    async def test_inbox_empty_when_no_tickets(self) -> None:
        """无 ticket：pipelines 空数组，不报错。"""
        server = self._load_server()
        cap = FakeStateCap(rows=[{"pipeline_id": "p1", "thread_id": "p1"}])
        payload = await self._handle(server, cap, {})
        assert payload["pipelines"] == []

    async def test_inbox_capability_missing_degrades(self) -> None:
        """pipeline-state 能力缺席：空 inbox（读面降级不崩）。"""
        server = self._load_server()
        server.plugin._capabilities.pop("pipeline-state", None)  # type: ignore[attr-defined]
        payload = await self._handle(server, FakeStateCap(), {})
        assert payload["pipelines"] == []


# ══ approval / security_check：category 参数透传 ══════════════════════


class FakeHiCap:
    """假 human-interaction capability：记录 create_choice 参数，wait 立即超时。"""

    def __init__(self, wait_result: dict[str, Any] | None = None):
        self.create_calls: list[dict[str, Any]] = []
        self._wait_result = wait_result or {"error": "timeout", "error_code": "INTERACTION_TIMEOUT"}

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "create_choice":
            self.create_calls.append(params)
            return {"request_id": "req-x1", "status": "pending"}
        if method == "wait_for_choice":
            return self._wait_result
        return {}


class TestCategoryPassthrough:
    """approval 传 decision、security_check 三处传 permission。"""

    def _approval_server(self) -> Any:
        return _load(_APPROVAL_DIR / "server.py", "approval_server_m1_cat")

    async def test_approval_create_choice_passes_decision(self) -> None:
        """approval.create_choice → human create_choice 带 category=decision。"""
        server = self._approval_server()
        hi = FakeHiCap()
        server.plugin._capabilities["human-interaction"] = hi  # type: ignore[attr-defined]
        server.plugin._capabilities.pop("event-bus", None)  # type: ignore[attr-defined]
        server.plugin._capabilities.pop("pipeline-executor", None)  # type: ignore[attr-defined]
        await server.create_choice(
            title="合并 12 个文件", options=["批准", "拒绝"],
            option_semantics=["approve_once", "deny"],
        )
        assert hi.create_calls, "create_choice 被调"
        assert hi.create_calls[0].get("category") == "decision"

    def _security_plugin(self) -> Any:
        return _load(_SECURITY_DIR / "plugin.py", "security_plugin_m1")

    async def test_security_check_approval_passes_permission(self) -> None:
        """security_check 安全审批（_await_approval）→ category=permission。"""
        plugin_mod = self._security_plugin()
        hi = FakeHiCap()
        plugin_mod.set_human_interaction_cap(hi)
        inst = plugin_mod.SecurityCheckPlugin()
        ctx = _make_security_ctx()
        await inst._await_approval(ctx, "bash", "危险命令", signatures=["sig-x"])
        assert hi.create_calls, "安全审批 create_choice 被调"
        assert hi.create_calls[0].get("category") == "permission"

    async def test_security_check_zone_grant_passes_permission(self) -> None:
        """security_check 写区授权卡（_authorize_grant_card）→ category=permission。"""
        plugin_mod = self._security_plugin()
        hi = FakeHiCap()
        plugin_mod.set_human_interaction_cap(hi)
        inst = plugin_mod.SecurityCheckPlugin()
        ctx = _make_security_ctx()
        await inst._authorize_grant_card(
            ctx, tool_name="bash", args={}, resolved=Path("/tmp/zone"),
            zone_updates={}, write=True,
        )
        assert hi.create_calls, "写区授权 create_choice 被调"
        assert hi.create_calls[0].get("category") == "permission"

    async def test_security_check_mode_switch_passes_permission(self) -> None:
        """security_check 模式切换确认（server._confirm_switch）→ category=permission。"""
        server = _load(_SECURITY_DIR / "server.py", "security_server_m1")
        hi = FakeHiCap()
        server.plugin._capabilities["human-interaction"] = hi  # type: ignore[attr-defined]
        await server._confirm_switch(session_id="s1", mode="auto", user_id="u1")
        assert hi.create_calls, "模式切换 create_choice 被调"
        assert hi.create_calls[0].get("category") == "permission"


def _make_security_ctx() -> Any:
    """security_check execute 上下文最小桩（state dict 形态，StateKeys 键值）。"""
    from types import SimpleNamespace

    return SimpleNamespace(state={
        "session_id": "s1",
        "user_id": "u1",
        "agent.id": "agent-1",
        "agent_level": "L2",
        "pipeline_id": "p1",
        "context.agent_name": "执行者",
        "raw_tool_calls": [],
    })
