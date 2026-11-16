# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 并发模型身份隔离测试（装机 382ec1ee 串写缺陷回归）。

契约（2026-10-27 用户裁定「模型身份随 state 走，与实例状态无关」）：
llm_core 单实例被多管道并发复用（灵汐主对话 + 派发的 code_writer 子任务
同进程交错），每次 execute 的模型身份必须来自**本次 execute 解析的绑定
快照**——调用参数（kwargs.model）与结果 state（llm_model/llm_provider/
llm_api_base/llm_usage.model）一律读快照；任一其他管道在本次调用生命期
内重绑实例（``_apply_model_from_state`` 踩踏共享字段）不得改变本次调用
的身份归属。

缺陷形态（修复前）：``_build_usage`` / 结果组装 / ``_call_llm`` kwargs
读 ``self._model/_provider/_api_base`` 实例字段，await 前后被并发 execute
的 ``_apply_binding`` 覆写 → 主对话被贴上子任务模型标签（llm_provider=
minimax 与绑定 glm 同帧并存），重试路径甚至会拿踩后的 model_id 发真请求。

加载：importlib 唯一模块名装载 plugin.py（裸名 ``plugin`` 会被兄弟插件
目录串扰）；``_config_models`` 经 shared/llm 目录平铺 import（追加 sys.path
尾部，不夺 llm_core 平铺名的首位——两目录均有 adapter.py）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

# _config_models 平铺模块只在 shared/llm 目录（追加到尾部：llm_core 平铺名
# adapter.py 必须仍命中本目录版本——llm 目录同名件会被误解析）。
if str(_LLM_DIR) not in sys.path:
    sys.path.append(str(_LLM_DIR))

_MOD_NAME = "llm_core_concurrent_identity_under_test"


def _load_plugin() -> Any:
    """加载 llm_core/plugin.py（唯一模块名，进程内缓存）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None, "cannot load llm_core plugin.py"
    assert spec.loader is not None, "cannot load llm_core plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin()
LLMCore = _mod.LLMCore
set_capability_caller = _mod.set_capability_caller


def _make_plugin(caller: Any) -> Any:
    set_capability_caller(caller)
    return LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = SimpleNamespace()
    ctx.state = state
    return ctx


def _model_entry(
    model_id: str, provider: str, api_base: str, **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "provider": provider,
        "model_name": model_id,
        "api_base": api_base,
        "api_key": f"key-{model_id}",
        "context_window": 64000,
        "default_params": {"temperature": 0.3},
    }
    entry.update(extra)
    return entry


def _set_llm_config(monkeypatch: Any, llm: dict[str, Any]) -> None:
    """注入 llm.yaml 视图（配置桥 _config_models._config["llm"]）。"""
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {"llm": llm})


def _two_model_config() -> dict[str, Any]:
    """两模型配置：model-a（glm 面）/ model-b（minimax 面），默认 model-a。"""
    return {
        "models": {
            "model-a": _model_entry("model-a", "prov-a", "https://a.example.com"),
            "model-b": _model_entry("model-b", "prov-b", "https://b.example.com"),
        },
        "defaults": {"chat": "model-a"},
    }


def _state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "pipeline-a",
        "run_id": "run-a",
    }
    state.update(extra)
    return state


def _usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "cached_tokens": 0,
    }


def _ok_response(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "streamed",
        "stream_id": "stream_test",
        "partial": None,
        "text": "ok",
        "tool_calls": [],
        "thinking_text": None,
        "usage": _usage(),
        "finish_reason": "stop",
    }
    result.update(overrides)
    return result


async def test_result_identity_immune_to_rebind_during_call(monkeypatch: Any) -> None:
    """调用在飞时并发 execute 重绑实例 → 本次结果的身份归属不变。

    复现生产形态：主对话（model-a）长调用在飞，派发子任务（model-b）在同
    一 llm_core 实例上 execute 重绑。修复前结果组装读实例字段 → 主对话被
    贴成 model-b（llm_provider=minimax 与绑定 glm 同帧并存）。
    """
    _set_llm_config(monkeypatch, _two_model_config())
    state_b = _state(
        pipeline_id="pipeline-b", run_id="run-b", model_id="model-b"
    )
    seen_models: list[str] = []

    class _StompingCaller:
        async def __call__(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            seen_models.append(params["args"]["model"])
            # 并发 execute 在 A 调用在飞期间重绑共享实例（生产踩踏点）
            plugin._apply_model_from_state(dict(state_b))
            return {"success": True, "data": _ok_response()}

    plugin = _make_plugin(_StompingCaller())
    result = await plugin.execute(_make_ctx(_state()))

    # 调用参数：本次 execute 解析的 model-a（绑定未被踩前已捕获）
    assert seen_models == ["model-a"]
    # 结果身份：全部归属 model-a，不被并发重绑改写
    assert result["llm_usage"]["model"] == "model-a"
    assert result["llm_usage"]["provider"] == "prov-a"
    assert result["llm_model"] == "model-a"
    assert result["llm_provider"] == "prov-a"
    assert result["llm_api_base"] == "https://a.example.com"
    assert result["context_window"] == 64000


async def test_call_kwargs_model_immune_to_rebind_before_build(monkeypatch: Any) -> None:
    """绑定应用后、kwargs 构建前被并发重绑（线程交错形态）→ 调用仍用本次绑定。

    生产对应形态：apply 与 kwargs 构建之间被并发 execute 插入重绑，修复前
    kwargs.model 读实例字段 → 真请求发到并发方的模型（00:45:03 实测 62 万
    token 真打 minimax）。
    """
    _set_llm_config(monkeypatch, _two_model_config())
    state_b = _state(
        pipeline_id="pipeline-b", run_id="run-b", model_id="model-b"
    )
    seen_models: list[str] = []

    class _RecordingCaller:
        async def __call__(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            seen_models.append(params["args"]["model"])
            return {"success": True, "data": _ok_response()}

    plugin = _make_plugin(_RecordingCaller())
    original_build_messages = plugin._build_messages

    def _stomping_build_messages(state: dict[str, Any]) -> list[dict[str, Any]]:
        # apply 之后、kwargs 构建之前的并发重绑（线程交错窗口）
        plugin._apply_model_from_state(dict(state_b))
        return original_build_messages(state)

    # 实例属性直挂（非类绑定）：execute 内 self._build_messages(state) 命中
    plugin._build_messages = _stomping_build_messages  # type: ignore[method-assign]
    result = await plugin.execute(_make_ctx(_state()))

    assert seen_models == ["model-a"]
    assert result["llm_usage"]["model"] == "model-a"
    assert result["llm_model"] == "model-a"


async def test_partial_result_identity_immune_to_rebind(monkeypatch: Any) -> None:
    """流中断半截落库路径的身份归属同样读快照（与成功路径同构）。"""
    _set_llm_config(monkeypatch, _two_model_config())
    state_b = _state(
        pipeline_id="pipeline-b", run_id="run-b", model_id="model-b"
    )
    partial_payload = {
        "text": "half",
        "thinking_text": "",
        "tool_calls": [],
        "usage": _usage(),
    }

    class _PartialStompingCaller:
        async def __call__(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            plugin._apply_model_from_state(dict(state_b))
            return {
                "success": True,
                "data": _ok_response(
                    status="error", partial=partial_payload
                ),
            }

    plugin = _make_plugin(_PartialStompingCaller())
    result = await plugin.execute(_make_ctx(_state()))

    assert result["llm_usage"]["model"] == "model-a"
    assert result["llm_usage"]["provider"] == "prov-a"
    assert result["llm_model"] == "model-a"
    assert result["llm_provider"] == "prov-a"
    assert result["llm_api_base"] == "https://a.example.com"


# ─────────────────── 流式超时配置透传（装机 2026-11-04 事故回归） ───────────────────


def _two_model_config_with_timeouts() -> dict[str, Any]:
    """在两模型配置上注入 llm.yaml 流式超时键（defaults + 模型条目覆盖）。"""
    cfg = _two_model_config()
    cfg["defaults"]["first_token_timeout"] = 90
    cfg["defaults"]["stream_idle_timeout"] = 120
    # 模型条目覆盖优先于 defaults（models.<id>.<key> > defaults.<key>）
    cfg["models"]["model-a"]["first_token_timeout"] = 33
    return cfg


async def test_timeout_config_flows_into_complete_stream_kwargs(
    monkeypatch: Any,
) -> None:
    """llm.yaml 流式超时配置 stream_idle_timeout 必须随 llm.complete_stream
    kwargs 上送；first_token_timeout 退役不上送（2026-11-04 用户裁定：吐字
    间隔超时含首 token——首块预算与吐字间隔同源，独立的更短首 token 预算
    会使「180s 含首 token」失真）。

    生产事故 2026-11-04（装机 e4bfd6538ed0）：binding 快照丢弃超时键，调用侧
    从不携带 → llm_service adapter 恒用内部兜底 600s，上游流中途冻结
    10min28s 才被判错，任务面死窗 10 分钟。模型条目覆盖优先于 defaults。
    """
    _set_llm_config(monkeypatch, _two_model_config_with_timeouts())
    seen_kwargs: list[dict[str, Any]] = []

    class _RecordingCaller:
        async def __call__(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            seen_kwargs.append(dict(params["args"]))
            return {"success": True, "data": _ok_response()}

    plugin = _make_plugin(_RecordingCaller())
    await plugin.execute(_make_ctx(_state()))

    assert seen_kwargs[0]["stream_idle_timeout"] == 120
    assert "first_token_timeout" not in seen_kwargs[0]  # 退役：不上送


async def test_legacy_binding_snapshot_without_timeout_fields_omits_keys(
    monkeypatch: Any,
) -> None:
    """旧绑定快照（无超时字段，装机存量形态）→ kwargs 不携带两键，adapter 落
    自身默认——升级期在飞管线不因快照缺字段报错。"""
    _set_llm_config(monkeypatch, _two_model_config())
    legacy_binding: dict[str, Any] = {
        "run_id": "run-a",
        "model_id": "model-a",
        "provider": "prov-a",
        "model_name": "model-a",
        "api_base": "https://a.example.com",
        "default_params": {"temperature": 0.3},
        "context_window": 64000,
        "call_timeout": 1800,
        "resolved_at": "2026-11-04T00:00:00+00:00",
    }
    seen_kwargs: list[dict[str, Any]] = []

    class _RecordingCaller:
        async def __call__(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            seen_kwargs.append(dict(params["args"]))
            return {"success": True, "data": _ok_response()}

    plugin = _make_plugin(_RecordingCaller())
    await plugin.execute(_make_ctx(_state(llm_model_binding=legacy_binding)))

    assert seen_kwargs[0]["model"] == "model-a"  # 确走快照复用路径
    assert "first_token_timeout" not in seen_kwargs[0]
    assert "stream_idle_timeout" not in seen_kwargs[0]
