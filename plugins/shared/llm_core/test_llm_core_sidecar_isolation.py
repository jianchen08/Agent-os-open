# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core sidecar 实例复用契约（per-pipeline 运行时配置隔离）。

同 sidecar 实例被多 agent 管道并发复用，async handler 不串行：空回复重试上限
曾以 ``_max_empty_retries`` 实例字段承载，A 管道在 LLM 调用 await 处让出后被
B 管道的 execute 复位/覆盖——A 的超限判定读到 B 的上限（task_reminder 197d
同范式）。现由 ``_resolve_max_empty_retries`` 解析成 execute 局部值贯穿本轮。

交叠点 = ``_call_llm`` 的能力调用（A 在此让出，B 整轮跑完，再放行 A）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

_MOD_NAME = "llm_core_sidecar_isolation_under_test"


def _load_plugin() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None, "cannot load llm_core plugin.py"
    assert spec.loader is not None, "cannot load llm_core plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _llm_model_config(monkeypatch: Any) -> None:
    """注入最小 llm.yaml 视图：模型空解析现在显式报错（本模块不测模型解析）。"""
    import _config_models

    monkeypatch.setattr(
        _config_models,
        "_config",
        {
            "llm": {
                "models": {
                    "stub-model": {
                        "provider": "stub",
                        "model_name": "stub-model",
                        "api_base": "https://stub.example.com",
                        "api_key": "stub-key",
                    }
                },
                "defaults": {"chat": "stub-model"},
            }
        },
    )


def _empty_response() -> dict[str, Any]:
    """合法信封 + 空回复（无文本、无工具调用）→ 触发空回复重试策略。"""
    return {
        "success": True,
        "data": {
            "status": "streamed",
            "stream_id": "stream_test",
            "partial": None,
            "text": None,
            "tool_calls": [],
            "thinking_text": None,
            "usage": {},
            "finish_reason": "stop",
        },
    }


class _GatedCaller:
    """按 pipeline_id 门控的能力调用桩（A 管道在 LLM 调用处等待）。"""

    def __init__(self, gated_pipeline: str, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._gated = gated_pipeline
        self._entered = entered
        self._release = release
        self.pipeline_ids: list[str] = []

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:  # noqa: ARG002
        assert method == "invoke"
        pipeline_id = str((params.get("_call_context") or {}).get("pipeline_id") or "")
        self.pipeline_ids.append(pipeline_id)
        if pipeline_id == self._gated:
            self._entered.set()
            await self._release.wait()
        return _empty_response()


def _state(pipeline_id: str, *, max_empty_retries: int | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": f"hi from {pipeline_id}"}],
        "streaming": True,
        "pipeline_id": pipeline_id,
    }
    if max_empty_retries is not None:
        state["plugin_configs"] = {"llm_core": {"max_empty_retries": max_empty_retries}}
    return state


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestEmptyRetryLimitPerPipeline:
    def test_interleaved_execute_keeps_own_limit(self) -> None:
        """A 管道上限 0（本轮即耗尽），B 管道用构造默认 2；A 在 LLM 调用处
        让出、B 整轮完成后，A 的判定必须仍按自己的 0 走耗尽。"""
        mod = _load_plugin()
        release = asyncio.Event()
        entered = asyncio.Event()
        caller = _GatedCaller("pipe-a", entered, release)
        mod.set_capability_caller(caller)
        try:
            plugin = mod.LLMCore(
                {"provider": "openai", "model_name": "deepseek-v3", "default_params": {}}
            )
            ctx_a = SimpleNamespace(state=_state("pipe-a", max_empty_retries=0))
            ctx_b = SimpleNamespace(state=_state("pipe-b"))

            async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
                task_a = asyncio.create_task(plugin.execute(ctx_a))
                await entered.wait()
                result_b = await plugin.execute(ctx_b)
                release.set()
                result_a = await task_a
                return result_a, result_b

            result_a, result_b = _run(scenario())
        finally:
            mod.set_capability_caller(None)

        assert result_b["llm_empty_streak"] == 1
        assert result_b["_has_new_llm_input"] is True, "B 管道默认上限 2 → 第 1 次仍重试"
        assert result_a["llm_empty_streak"] == 1
        assert result_a.get("llm_empty_exhausted") is True, (
            "A 管道上限 0 → 本轮即耗尽；读到 B 的默认 2 说明实例字段串台"
        )
        assert result_a["_has_new_llm_input"] is False
        assert caller.pipeline_ids == ["pipe-a", "pipe-b"], "A 的调用先入（按调用顺序记录）"

    def test_limit_resolution_does_not_touch_instance(self) -> None:
        """解析入口只读 config/state，不产生实例字段（并发安全的形态约束）。"""
        mod = _load_plugin()
        mod.set_capability_caller(_GatedCaller("none", asyncio.Event(), asyncio.Event()))
        try:
            plugin = mod.LLMCore(
                {
                    "provider": "openai",
                    "model_name": "deepseek-v3",
                    "default_params": {},
                    "max_empty_retries": 5,
                }
            )
            assert not hasattr(plugin, "_max_empty_retries")
            assert plugin._resolve_max_empty_retries(
                SimpleNamespace(state=_state("pipe-x"))
            ) == 5
            assert plugin._resolve_max_empty_retries(
                SimpleNamespace(state=_state("pipe-y", max_empty_retries=1))
            ) == 1
        finally:
            mod.set_capability_caller(None)
