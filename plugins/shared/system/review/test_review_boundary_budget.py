# @feature: FP-0.2.二 复盘边界登记与预算 | @vision: V3 可嵌入 | @ci: none-local
"""trigger_review 被复盘集合登记与预算 TDD 测试（§10.5 裁定实施，工作包 C）。

裁定依据：docs/working/评估体系全景与自进化流水线_20260924.md §10.5——
- 查询边界：被复盘对象集合在派发时登记进复盘管道 state
  （review.target_pipelines / review.budget_pipelines）；
- 复盘预算：单复盘管道可纳入的管道数上限（可配置，缺省 5），
  超出拒绝派发（fail-closed，由调用方拆分）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.capability import CapabilityHandle

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    mod_name = "review_server_wpC_test"
    module_path = _PLUGIN_DIR / "server.py"
    assert module_path.exists(), f"server.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    module = _load_module()
    module._reports.clear()
    if "chat" in module.plugin._capabilities:
        module.plugin._capabilities.pop("chat", None)
    return module


def _inject_chat(mod: Any, *, pipeline_id: str = "pipe_wpC_1") -> dict[str, Any]:
    """注入 fake chat 能力并捕获派发参数（供 state 登记断言）。"""
    captured: dict[str, Any] = {}

    async def call_fn(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if method == "send_message" and not any(
            str(k).startswith("task.owned.") for k in (params.get("state") or {})
        ):
            captured.update(params)
        return {"pipeline_id": pipeline_id}

    mod.plugin._capabilities["chat"] = CapabilityHandle("chat", call_fn=call_fn)
    return captured


class TestTargetPipelinesRegistration:
    async def test_single_task_registers_singleton_set(self, mod: Any) -> None:
        """单任务复盘：登记集合 = {task_id}，预算随行。"""
        captured = _inject_chat(mod)

        r = await mod.trigger_review(task_id="task-c1", summary="s")

        assert r["status"] == "running"
        state = captured["state"]
        assert state["review.target_pipelines"] == ["task-c1"]
        assert state["review.budget_pipelines"] == mod._MAX_PIPELINES_PER_REVIEW

    async def test_multi_pipeline_list_merges_and_dedups(self, mod: Any) -> None:
        """多管道复盘：task_id + 显式清单去重合并登记。"""
        captured = _inject_chat(mod)

        r = await mod.trigger_review(
            task_id="task-c2",
            summary="s",
            pipeline_ids=["task-c2", "task-c3", "task-c4"],
        )

        assert r["status"] == "running"
        state = captured["state"]
        assert state["review.target_pipelines"] == ["task-c2", "task-c3", "task-c4"]


class TestReviewBudget:
    async def test_over_budget_rejects_dispatch(self, mod: Any) -> None:
        """超预算：拒绝派发（fail-closed），不产生报告。"""
        _inject_chat(mod)

        r = await mod.trigger_review(
            task_id="task-c5",
            summary="s",
            pipeline_ids=[f"task-x{i}" for i in range(mod._MAX_PIPELINES_PER_REVIEW + 2)],
        )

        assert r["status"] == "rejected"
        assert "budget" in r.get("error", "").lower() or "预算" in r.get("error", "")
        assert not any(
            rep.get("task_id") == "task-c5" for rep in mod._reports.values()
        )

    async def test_budget_configurable_override(self, mod: Any) -> None:
        """预算可配置：plugin_configs 覆盖缺省上限（3 管道恰在预算内）。"""
        captured = _inject_chat(mod)
        mod.plugin.get_config = lambda: {"max_pipelines_per_review": 3}

        r = await mod.trigger_review(
            task_id="task-c6", summary="s", pipeline_ids=["task-c7", "task-c8"]
        )

        assert r["status"] == "running"
        assert captured["state"]["review.budget_pipelines"] == 3
