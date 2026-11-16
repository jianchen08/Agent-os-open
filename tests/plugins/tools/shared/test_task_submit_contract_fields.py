# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""task_submit 契约面字段扩围测试（第 5 项编排链接线族片 1，E0-20-D1/E0-24/25 字段面）。

input_schema 新增两个可选字段（只增不删，语义消费在片 3）：
- allowed_paths：任务书改动授权路径清单（消费方=scope_consistency）；
- signed_acceptance_tests：签发验收测试清单，元素 {path, frozen_version, issuer}。

覆盖四类路径：
1. 合法形态通过（字段可提交、派发不受影响）；
2. 缺 issuer 等非法形态拒绝且错误信息指明具体字段名；
3. allowed_paths 元素非 string 拒绝；
4. 两字段缺省/空数组=行为与旧版完全一致（零行为变更兼容面）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "task_submit"
_SYSTEM_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "shared"

for _d in [_SYSTEM_ROOT, _SYSTEM_ROOT / "tasks", _SYSTEM_ROOT / "channel_api"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_contract_fields_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TS_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


@pytest.fixture(autouse=True)
def _birth_ws(mod: Any) -> Any:
    """出生协议第三段（提交期同步建工作空间）准备器替身——驱动真实派发的
    用例必注入（ADR-2026-10-17-task-birth-workspace-inclusion：无跳过路径）。
    """
    from tests import _task_birth_workspace as _birth_ws_mod

    _birth_ws_mod.inject_preparer(mod)
    yield
    _birth_ws_mod.reset_preparer(mod)


class _FakeSender:
    """记录 chat.send_message 参数的派发器 fake（统一出生协议三段式响应）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        if params.get("create"):
            return {"status": "created", "pipeline_id": "a1b2c3d4e5f6"}
        if params.get("no_dispatch"):
            return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}
        return {"status": "dispatched", "pipeline_id": params.get("pipeline_id", "")}


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal_title": "契约面字段扩围",
        "goal_description": "提交带 allowed_paths 与 signed_acceptance_tests 的任务书",
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
        "session_id": "sess-1",
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    """构造工具实例并 stub 纯参数校验（与既有边界测试同模式，断言参数面）。"""
    tool = mod.TaskSubmitTool()

    async def _ok(t, l):
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]

    async def _base_config(target_id: str) -> tuple[dict[str, Any] | None, None]:
        return (
            {
                "level": "L2",
                "is_active": True,
                "tool_ids": ["file_read"],
                "system_prompt": "stub-persona",
            },
            None,
        )

    tool._get_agent_base_config = _base_config  # type: ignore[method-assign]
    return tool


async def _run(mod: Any, inputs: dict) -> Any:
    """执行一次提交并复位全局 sender（不泄漏到后续测试）。"""
    sender = _FakeSender()
    mod.set_chat_sender(sender)
    tool = _make_tool(mod)
    try:
        return await tool.execute(inputs), sender
    finally:
        mod._chat_sender = None


def _signed_entry(**over: Any) -> dict:
    entry = {
        "path": "tests/signed/test_scope_guard.py",
        "frozen_version": "v1.0",
        "issuer": "调度方",
    }
    entry.update(over)
    return {k: v for k, v in entry.items() if v is not _ABSENT}


class _ABSENT:
    """哨兵：显式删除某键（区别于空值）。"""


# ── schema 契约面 ─────────────────────────────────────


class TestContractSchema:
    async def test_schema_declares_both_optional_fields(self, mod: Any) -> None:
        """input_schema 声明两新字段且均不在 required（可选，只增不删）。"""
        definition = mod.TaskSubmitTool.get_tool_definition()
        props = definition.input_schema["properties"]
        assert "allowed_paths" in props
        assert "signed_acceptance_tests" in props
        assert props["allowed_paths"]["type"] == "array"
        assert props["allowed_paths"]["items"]["type"] == "string"
        sat = props["signed_acceptance_tests"]
        assert sat["type"] == "array"
        entry_props = sat["items"]["properties"]
        assert set(entry_props) == {"path", "frozen_version", "issuer"}
        assert sat["items"]["required"] == ["path", "frozen_version", "issuer"]
        assert "allowed_paths" not in definition.input_schema.get("required", [])
        assert "signed_acceptance_tests" not in definition.input_schema.get("required", [])


# ── 合法形态通过 ─────────────────────────────────────


class TestValidContractFields:
    async def test_valid_fields_submit_and_dispatch(self, mod: Any) -> None:
        """两字段合法形态齐备 → 提交成功、派发链路不受影响。"""
        inputs = _base_inputs(
            allowed_paths=["plugins/shared/task_submit/", "docs/working/"],
            signed_acceptance_tests=[
                _signed_entry(),
                {"path": "tests/signed/test_other.py", "frozen_version": "v2.1", "issuer": "审查线"},
            ],
        )
        r, sender = await _run(mod, inputs)
        assert r.success, r.error
        assert len(sender.calls) == 5  # 出生四段 + no_dispatch 登记（与既有基线一致）

    async def test_empty_arrays_pass(self, mod: Any) -> None:
        """空数组=合法（语义留给消费方）。"""
        r, sender = await _run(mod, _base_inputs(allowed_paths=[], signed_acceptance_tests=[]))
        assert r.success, r.error
        assert len(sender.calls) == 5


# ── allowed_paths 形态校验 ────────────────────────────


class TestAllowedPathsValidation:
    async def test_non_string_element_rejected_with_field_name(self, mod: Any) -> None:
        """元素非 string → 拒绝，错误信息指明 allowed_paths 字段与索引。"""
        for bad in (123, ["docs/a.md", 456], ["docs/a.md", {"p": 1}]):
            r, _ = await _run(mod, _base_inputs(allowed_paths=bad))
            assert not r.success, f"{bad!r} 应被拒绝"
            assert "allowed_paths" in r.error, r.error

    async def test_blank_string_element_rejected(self, mod: Any) -> None:
        """空串/纯空白元素（非 path 形态）→ 拒绝。"""
        r, _ = await _run(mod, _base_inputs(allowed_paths=["docs/a.md", "   "]))
        assert not r.success
        assert "allowed_paths" in r.error, r.error

    async def test_non_array_value_rejected(self, mod: Any) -> None:
        """非数组形态（如裸字符串）→ 拒绝且错误信息指明字段。"""
        r, _ = await _run(mod, _base_inputs(allowed_paths="docs/a.md"))
        assert not r.success
        assert "allowed_paths" in r.error, r.error


# ── signed_acceptance_tests 形态校验 ─────────────────


class TestSignedAcceptanceTestsValidation:
    async def test_missing_issuer_rejected_with_field_name(self, mod: Any) -> None:
        """缺 issuer → 拒绝，错误信息指明 issuer 字段（任务书点名场景）。"""
        inputs = _base_inputs(
            signed_acceptance_tests=[_signed_entry(issuer=_ABSENT)],
        )
        r, _ = await _run(mod, inputs)
        assert not r.success
        assert "issuer" in r.error, r.error

    async def test_missing_path_rejected_with_field_name(self, mod: Any) -> None:
        """缺 path → 拒绝，错误信息指明 path 字段。"""
        inputs = _base_inputs(
            signed_acceptance_tests=[_signed_entry(path=_ABSENT)],
        )
        r, _ = await _run(mod, inputs)
        assert not r.success
        assert "path" in r.error, r.error

    async def test_missing_frozen_version_rejected_with_field_name(self, mod: Any) -> None:
        """缺 frozen_version → 拒绝，错误信息指明 frozen_version 字段。"""
        inputs = _base_inputs(
            signed_acceptance_tests=[_signed_entry(frozen_version=_ABSENT)],
        )
        r, _ = await _run(mod, inputs)
        assert not r.success
        assert "frozen_version" in r.error, r.error

    async def test_blank_issuer_rejected(self, mod: Any) -> None:
        """issuer 空串/纯空白/非串 → 拒绝（签发方必须可追溯）。"""
        for bad_issuer in ("", "   ", 42, None):
            inputs = _base_inputs(
                signed_acceptance_tests=[_signed_entry(issuer=bad_issuer)],
            )
            r, _ = await _run(mod, inputs)
            assert not r.success, f"issuer={bad_issuer!r} 应被拒绝"
            assert "issuer" in r.error, r.error

    async def test_non_dict_element_rejected(self, mod: Any) -> None:
        """元素非对象 → 拒绝且错误信息指明字段。"""
        r, _ = await _run(mod, _base_inputs(signed_acceptance_tests=["tests/signed/t.py"]))
        assert not r.success
        assert "signed_acceptance_tests" in r.error, r.error

    async def test_non_array_value_rejected(self, mod: Any) -> None:
        """非数组形态 → 拒绝且错误信息指明字段。"""
        r, _ = await _run(mod, _base_inputs(signed_acceptance_tests={"path": "x"}))
        assert not r.success
        assert "signed_acceptance_tests" in r.error, r.error


# ── 缺省兼容（零行为变更）────────────────────────────


class TestBackwardCompat:
    async def test_absent_fields_unchanged_dispatch(self, mod: Any) -> None:
        """不带两字段（旧版调用）→ 行为与旧版完全一致：成功且完整派发。"""
        r, sender = await _run(mod, _base_inputs())
        assert r.success, r.error
        assert len(sender.calls) == 5
        assert sender.calls[0]["state"]["task.goal"] == "契约面字段扩围"


# ── 任务记录读回（片 1 补刀：切分方案验收判据「字段可从任务记录读回」）──


class TestBirthStateReadback:
    async def test_fields_written_to_birth_state(self, mod: Any) -> None:
        """带两新字段提交 → birth_state 含 task.allowed_paths / task.signed_
        acceptance_tests，值=校验后的入参原样（片 3 params 单源装配读数源）。"""
        allowed = ["plugins/shared/task_submit/", "docs/working/"]
        signed = [
            _signed_entry(),
            {"path": "tests/signed/test_other.py", "frozen_version": "v2.1", "issuer": "审查线"},
        ]
        r, sender = await _run(
            mod,
            _base_inputs(allowed_paths=allowed, signed_acceptance_tests=signed),
        )
        assert r.success, r.error
        state = sender.calls[0]["state"]
        assert state["task.allowed_paths"] == allowed
        assert state["task.signed_acceptance_tests"] == signed

    async def test_absent_fields_not_written_to_birth_state(self, mod: Any) -> None:
        """缺省（不带新字段）→ birth_state 不含新键（零行为变更锚，键只增不删）。"""
        r, sender = await _run(mod, _base_inputs())
        assert r.success, r.error
        state = sender.calls[0]["state"]
        assert "task.allowed_paths" not in state
        assert "task.signed_acceptance_tests" not in state

    async def test_empty_arrays_not_written_to_birth_state(self, mod: Any) -> None:
        """空数组=无有效载荷 → 不写记录键（与缺省同口径，在场且有值才写）。"""
        r, sender = await _run(mod, _base_inputs(allowed_paths=[], signed_acceptance_tests=[]))
        assert r.success, r.error
        state = sender.calls[0]["state"]
        assert "task.allowed_paths" not in state
        assert "task.signed_acceptance_tests" not in state
