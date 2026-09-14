# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""approval / human 簇未覆盖分支补测（簇 J）。

按 HEAD 实测缺行钉行为契约：

approval/server.py
- ``_record_decision`` 的 TTL 清扫与硬上限逐出（第 239-243 行）：条目超过
  ``_DECISIONS_MAX_ENTRIES`` 时按 ts 逐出最旧，新写入恒在、长度恒 ≤ 上限；
  过期条目（ts 早于 TTL 窗口）在下一次写入时清除。
- ``submit`` 的"无挂起审批"分支（第 558 行）：request_id 无 _suspended 记录
  且无终态决策 → 明确 error + request_id（不伪造成功）。
- ``_on_load``（第 587 行）：生命周期钩子可 await、零副作用。

human/server.py
- ``human_interaction`` 模式校验与别名（第 318-325 行）：mode 缺失/非法枚举值
  一律在校验处拒绝；``type`` 别名映射到 mode（不绕过枚举校验）；
  ``message`` 别名补 ``title``（两个别名各自独立生效）。

human/service.py
- ``wait_for_choice`` 的请求不存在分支（第 336 行）：事件表已有该 id 而请求表
  缺失（登记不一致）时抛 ValueError——区别于"事件缺失但请求在"的补建事件路径。

★ 死代码披露（不改生产码，仅记录）：``human/server.py:338``
``return {"error": f"不支持的交互模式: {mode}"}`` 不可达——其上方第 324 行
守卫 ``if mode not in ("choice", "conversation", "notification")`` 已把所有
非枚举值提前拒绝，能走到第 332-337 行分派的 mode 必为三个枚举之一。本批按
"不可达防御分支"记录，测试钉守卫行为本身（拒绝文案 + 不落入任何分支）。

不可达/防御性残留（逐条说明，勿硬凑）：
1. ``approval/server.py:242`` 的 ``break`` 结构不可达：``min(..., default=None)``
   返回 None 要求 ``_decisions`` 为空，而循环守卫 ``len(_decisions) >= 4096``
   保证非空（既有 test_approval_server_gaps 已同款披露，属纯防御分支）。
2. ``human/server.py:338`` 见上方死代码披露（守卫前置导致的结构性不可达）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_APPROVAL_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "approval"
_HUMAN_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "human"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"

for _p in (_APPROVAL_DIR, _HUMAN_DIR, _HUMAN_DIR.parent, _SHARED_ROOT, _SDK_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(module_path: Path, name: str) -> Any:
    """按显式路径加载模块（唯一名，模块级状态互不污染）。"""
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None and spec.loader is not None, f"cannot load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def approval() -> Any:
    """全新 approval server 模块（_decisions/_suspended 隔离）。"""
    return _load(_APPROVAL_DIR / "server.py", "approval_server_gap_under_test")


# ══════════════════ approval._record_decision 治理面 ══════════════════


class TestDecisionEviction:
    def test_hard_cap_evicts_oldest_and_keeps_newest(
        self, approval: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """硬上限：逐出按 ts 最旧者，长度收敛到上限且最新写入在表。"""
        monkeypatch.setattr(approval, "_DECISIONS_MAX_ENTRIES", 3)
        clock = {"t": 1_000.0}
        monkeypatch.setattr(approval.time, "time", lambda: clock["t"])

        for i in range(5):
            clock["t"] += 1.0
            approval._record_decision(f"req-{i}", {"approved": bool(i), "resumed": False})

        assert len(approval._decisions) == 3
        assert "req-4" in approval._decisions
        assert "req-0" not in approval._decisions, "最旧条目先被逐出"

    def test_ttl_sweep_clears_expired_entries(
        self, approval: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TTL 清扫：写入时清除超出决策重读窗口的历史条目（性质：新条目保留）。"""
        monkeypatch.setattr(approval, "_DECISIONS_MAX_ENTRIES", 100)
        clock = {"t": 10_000.0}
        monkeypatch.setattr(approval.time, "time", lambda: clock["t"])

        approval._record_decision("old", {"approved": True, "resumed": True})
        clock["t"] += approval._DECISIONS_TTL_SECONDS + 1
        approval._record_decision("fresh", {"approved": False, "resumed": False})

        assert "old" not in approval._decisions
        assert "fresh" in approval._decisions
        assert approval._decisions["fresh"]["ts"] == clock["t"]

    def test_decision_entry_is_copy_without_mutating_caller(self, approval: Any) -> None:
        """性质：入库是副本并盖 ts，调用方传入的 dict 不被改动。"""
        source = {"approved": True, "resumed": False}
        approval._record_decision("copy-check", source)

        assert "ts" not in source
        assert approval._decisions["copy-check"]["ts"] > 0
        assert approval._decisions["copy-check"]["approved"] is True


# ══════════════════ approval.submit 无挂起 / 生命周期 ══════════════════


class TestSubmitUnknownRequest:
    async def test_unknown_request_reports_error(self, approval: Any) -> None:
        """既无挂起记录又无终态决策 → error + request_id（不伪造 resolved）。"""
        out = await approval.submit("never-seen", "approve")

        assert out == {
            "error": "no suspended approval for this request_id",
            "request_id": "never-seen",
        }

    async def test_decision_for_unknown_id_short_circuits(self, approval: Any) -> None:
        """对照：终态决策在表时直接重读，不落"无挂起"分支。"""
        approval._record_decision("decided", {"approved": True, "reason": "earlier", "resumed": True})

        out = await approval.submit("decided", "approve")

        assert out["status"] == "resolved" and out["resumed"] is True

    async def test_on_load_hook_awaitable_and_side_effect_free(self, approval: Any) -> None:
        """_on_load：可 await、不改挂起表与决策表（纯生命周期留痕）。"""
        approval._suspended["keep"] = {"suspend_handle": None, "run_id": None, "created_at": 0.0}
        before_suspended = dict(approval._suspended)
        before_decisions = dict(approval._decisions)

        assert await approval._on_load({}) is None

        assert approval._suspended == before_suspended
        assert approval._decisions == before_decisions


# ══════════════════ human.server 模式分发兜底 ══════════════════


@pytest.fixture
def human_server() -> Any:
    """全新 human server 模块（_service 全局隔离）。"""
    return _load(_HUMAN_DIR / "server.py", "human_server_gap_under_test")


class TestHumanModeDispatch:
    @pytest.mark.parametrize("bad_mode", ["broadcast", "", "notify", "CHOICE"])
    async def test_unknown_mode_rejected_at_guard(
        self, human_server: Any, monkeypatch: pytest.MonkeyPatch, bad_mode: str
    ) -> None:
        """枚举外取值（四种有区分度输入）在校验处拒绝，不落入任何分派分支。

        钉守卫契约：错误文案含取值与合法枚举，且三个 _do_* 分派函数零调用。
        """
        monkeypatch.setattr(human_server, "_service", object())
        called: list[str] = []
        for name in ("_do_notification", "_do_choice", "_do_conversation"):
            monkeypatch.setattr(human_server, name, lambda *_a, _n=name, **_k: called.append(_n))

        out = await human_server.human_interaction(mode=bad_mode, title="t")

        assert "mode 必填" in out["error"]
        assert called == [], "非法 mode 不得进入任何模式分支"

    async def test_missing_mode_rejected_before_service_wait(
        self, human_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：mode 缺失同样在校验处拒绝（服务未注入也不影响判定顺序）。"""
        monkeypatch.setattr(human_server, "_service", object())

        out = await human_server.human_interaction(title="t")

        assert "mode 必填" in out["error"]

    async def test_type_alias_maps_to_mode_without_bypassing_guard(
        self, human_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """type 别名映射为 mode 后仍走同一枚举校验（别名不绕过守卫）。"""
        monkeypatch.setattr(human_server, "_service", object())

        out = await human_server.human_interaction(type="streaming", title="t")

        assert "mode 必填" in out["error"]

    async def test_message_alias_fills_title(
        self, human_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """message 别名补 title：notification 分支收到 title 而非丢空。"""
        monkeypatch.setattr(human_server, "_service", object())
        seen: list[dict[str, Any]] = []

        async def _fake_notification(kwargs: dict[str, Any], session_id: str) -> dict[str, Any]:
            seen.append(dict(kwargs))
            return {"status": "sent"}

        monkeypatch.setattr(human_server, "_do_notification", _fake_notification)

        out = await human_server.human_interaction(mode="notification", message="别名标题")

        assert out == {"status": "sent"}
        assert seen[0]["title"] == "别名标题"


# ══════════════════ human.service wait_for_choice 请求缺失 ══════════════════


class TestWaitForChoiceUnknownRequest:
    async def test_event_absent_and_request_absent_raises(self) -> None:
        """事件表与请求表都无该 id → ValueError（不静默等待）。"""
        service = _load(_HUMAN_DIR / "service.py", "human_service_gap_under_test")

        svc = service.HumanInteractionService()
        with pytest.raises(ValueError, match="请求不存在"):
            await svc.wait_for_choice("ghost-request", timeout=0.01)

    async def test_event_present_but_request_record_absent_raises(self) -> None:
        """登记不一致（事件在、请求记录不在）→ 同样抛 ValueError。

        该分支守卫"等待方与登记表脱节"：待唤醒对象存在却无请求记录可读，
        继续等待必然读到空 message_data，故 fail-fast。
        """
        service = _load(_HUMAN_DIR / "service.py", "human_service_gap_under_test")

        svc = service.HumanInteractionService()
        rid = await svc.create_choice_request(
            "s1", "t1", "tab1", "标题", description="描述", options=[{"id": "1", "label": "批准"}]
        )
        assert rid in svc._pending_events and rid in svc._requests
        svc._requests.pop(rid)  # 模拟请求记录丢失而事件仍在

        with pytest.raises(ValueError, match="请求不存在"):
            await svc.wait_for_choice(rid, timeout=0.01)

    async def test_event_missing_but_request_present_creates_event(self) -> None:
        """对照分支：请求在但事件缺失 → 补建事件并正常等待（超时抛业务异常）。"""
        service = _load(_HUMAN_DIR / "service.py", "human_service_gap_under_test")

        svc = service.HumanInteractionService()
        rid = await svc.create_choice_request(
            "s1", "t1", "tab1", "标题", description="描述", options=[{"id": "1", "label": "批准"}]
        )
        svc._pending_events.pop(rid)  # 模拟事件登记丢失

        with pytest.raises(service.InteractionTimeoutError):
            await svc.wait_for_choice(rid, timeout=0.01)
