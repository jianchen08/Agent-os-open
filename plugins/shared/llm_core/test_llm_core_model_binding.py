# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 模型绑定快照（管线级粘性）测试。

契约（2026-10-20 用户裁定「模型配置改动 = 下一管道生效」；2026-10-23 用户裁定
升格为管线级绑定——快照跨 run 复用，显式选择才换绑）：

- 管线首次解析**快照模型解析结果**（``state.llm_model_binding``，run_id 仅为
  落定来源留痕）；快照存解析结果
  （provider/model_name/api_base/default_params/context_window），
  不含凭证（api_key 不落 state）。
- **管线粘性**：同管线后续 run 沿用快照——defaults.chat / model_tier 热重载
  漂移不在会话中途换模型（382ec1ee 实测三根因之一），模型被改名/移除也不当场
  杀死在飞管道。
- **唯一换绑入口** = 本 run 携带显式 ``model_id`` 且与绑定不同（前端模型
  选择器最高优先级覆盖管道配置）；空值/同值沿用绑定。
- 模型改名迁移走**显式映射**（llm.yaml ``aliases`` 顶层别名表 /
  ``models.<id>.superseded_by`` 墓碑声明）：命中则按映射**显式切换并留痕**
  （绑定记 ``migrated_from`` + ``migration_source``，state 落迁移留痕键）。
- 映射未命中且模型不在可用列表 → **仍 fail-closed**（拒绝以其他模型静默回落，
  错误文本带 ``MODEL_UNAVAILABLE`` 标记供下游分类）。

加载：importlib 唯一模块名装载 plugin.py（裸名 ``plugin`` 会被兄弟插件目录
串扰）；``_config_models`` 经 shared/llm 目录平铺 import（追加到 sys.path 尾部，
不夺 llm_core 平铺名的首位——两目录均有 adapter.py）。
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
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

# _config_models 平铺模块只在 shared/llm 目录（追加到尾部：llm_core 平铺名
# adapter.py 必须仍命中本目录版本——llm 目录同名件会被误解析）。
if str(_LLM_DIR) not in sys.path:
    sys.path.append(str(_LLM_DIR))

_MOD_NAME = "llm_core_model_binding_under_test"


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
MODEL_UNAVAILABLE_MARKER = _mod.MODEL_UNAVAILABLE_MARKER


class _FakeCaller:
    """伪 capability caller：回放 ok 响应并记录调用参数。"""

    def __init__(self, result: Any = None) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        return self._result


def _ok_response(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "streamed",
        "stream_id": "stream_test",
        "partial": None,
        "text": "ok",
        "tool_calls": [],
        "thinking_text": None,
        "usage": {},
        "finish_reason": "stop",
    }
    result.update(overrides)
    return result


def _make_plugin(caller: Any) -> Any:
    set_capability_caller(caller)
    return LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = SimpleNamespace()
    ctx.state = state
    return ctx


def _model_entry(model_id: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "provider": "prov",
        "model_name": model_id,
        "api_base": f"https://{model_id}.example.com",
        "api_key": "secret-key",
        "context_window": 64000,
        "default_params": {"temperature": 0.3},
    }
    entry.update(extra)
    return entry


def _set_llm_config(monkeypatch: Any, llm: dict[str, Any]) -> None:
    """注入 llm.yaml 视图（配置桥 _config_models._config["llm"]）。"""
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {"llm": llm})


def _state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "test-binding",
        "run_id": "run-1",
    }
    state.update(extra)
    return state


def _merge(state: dict[str, Any], updates: dict[str, Any]) -> None:
    """模拟引擎 merge：插件 state_updates 平插进 state。"""
    for key, value in updates.items():
        if key != "messages":
            state[key] = value


# ─────────────────── 快照写入 ───────────────────


async def test_run_start_snapshots_model_resolution(monkeypatch: Any) -> None:
    """run 首次解析 → 快照落 state（解析结果全量，不含凭证）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_state()))

    binding = result["llm_model_binding"]
    assert binding["model_id"] == "model-a"
    assert binding["provider"] == "prov"
    assert binding["model_name"] == "model-a"
    assert binding["api_base"] == "https://model-a.example.com"
    assert binding["default_params"] == {"temperature": 0.3}
    assert binding["context_window"] == 64000
    assert binding["run_id"] == "run-1"
    # 凭证不落 state（state 会进轨迹/前端投影面）
    assert "api_key" not in binding
    assert "secret-key" not in str(binding)


# ─────────────────── 在飞保护（问题 1）───────────────────


async def test_inflight_run_keeps_startup_binding_when_model_removed(monkeypatch: Any) -> None:
    """热重载移除模型名 → 在飞 run 仍用启动时快照继续调用（不被配置改动杀死）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    first = await plugin.execute(_make_ctx(state))
    _merge(state, first)
    assert state["llm_model_binding"]["model_id"] == "model-a"

    # 热重载：model-a 被改名移除，defaults 切到 model-b
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    second = await plugin.execute(_make_ctx(state))

    assert caller.calls[1][1]["args"]["model"] == "model-a"
    assert second["llm_model"] == "model-a"
    # 绑定未被改写（仍指启动时模型）
    assert state["llm_model_binding"]["model_id"] == "model-a"


async def test_new_run_after_hot_reload_uses_new_config(monkeypatch: Any) -> None:
    """新 run（无快照）→ 用热重载后的新配置（改动=下一管道生效）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_state(run_id="run-2")))

    assert caller.calls[0][1]["args"]["model"] == "model-b"
    assert result["llm_model_binding"]["model_id"] == "model-b"


async def test_inflight_run_not_switched_by_defaults_change(monkeypatch: Any) -> None:
    """在飞 run 不随 defaults.chat 改指向漂移（快照锁定启动时模型）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a"), "model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    _merge(state, await plugin.execute(_make_ctx(state)))

    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a"), "model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    await plugin.execute(_make_ctx(state))

    assert caller.calls[1][1]["args"]["model"] == "model-a"


async def test_inflight_run_keeps_startup_params(monkeypatch: Any) -> None:
    """在飞 run 的采样参数以启动时快照为准（配置改动=下一管道生效）。

    同模型改参在**新一轮解析**时生效（无绑定快照）；已有绑定的在飞 run 不受
    配置改动影响——参数改动留给下一管道/下一 run。
    """
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    first = await plugin.execute(_make_ctx(state))
    _merge(state, first)
    assert caller.calls[0][1]["args"]["temperature"] == 0.3

    _set_llm_config(
        monkeypatch,
        {
            "models": {
                "model-a": _model_entry("model-a", default_params={"temperature": 0.9}),
            },
            "defaults": {"chat": "model-a"},
        },
    )
    await plugin.execute(_make_ctx(state))

    assert caller.calls[1][1]["args"]["temperature"] == 0.3, "在飞 run 用启动时快照的参数"


async def test_binding_from_previous_run_reused_across_runs(monkeypatch: Any) -> None:
    """管线粘性：跨 run 复用绑定快照（run_id 仅为落定留痕，不匹配不弃用）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a"), "model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    stale = {
        "run_id": "run-old",
        "model_id": "model-a",
        "provider": "prov",
        "model_name": "model-a",
        "api_base": "https://model-a.example.com",
        "default_params": {},
        "context_window": 64000,
    }

    result = await plugin.execute(_make_ctx(_state(run_id="run-new", llm_model_binding=stale)))

    # defaults.chat=model-b 漂移不换道：沿用上一 run 绑定的 model-a
    assert caller.calls[0][1]["args"]["model"] == "model-a"
    assert result.get("llm_model_binding") is None, "绑定未变化零写入"


async def test_explicit_model_id_switches_binding(monkeypatch: Any) -> None:
    """显式 model_id 且与绑定不同 → 最高优先级换绑（选择器覆盖管道配置）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a"), "model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    bound = {
        "run_id": "run-old",
        "model_id": "model-a",
        "provider": "prov",
        "model_name": "model-a",
        "api_base": "https://model-a.example.com",
        "default_params": {},
        "context_window": 64000,
    }

    result = await plugin.execute(
        _make_ctx(_state(run_id="run-new", llm_model_binding=bound, model_id="model-b"))
    )

    assert caller.calls[0][1]["args"]["model"] == "model-b"
    assert result["llm_model_binding"]["model_id"] == "model-b"
    assert result["llm_model_binding"]["run_id"] == "run-new"


async def test_explicit_model_id_same_value_reuses_binding(monkeypatch: Any) -> None:
    """显式 model_id 与绑定同值 → 沿用绑定（每条消息重复携带同选择不算换绑）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a"), "model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    bound = {
        "run_id": "run-old",
        "model_id": "model-a",
        "provider": "prov",
        "model_name": "model-a",
        "api_base": "https://model-a.example.com",
        "default_params": {"temperature": 0.3},
        "context_window": 64000,
    }

    result = await plugin.execute(
        _make_ctx(_state(run_id="run-new", llm_model_binding=bound, model_id="model-a"))
    )

    assert caller.calls[0][1]["args"]["model"] == "model-a"
    assert result.get("llm_model_binding") is None, "同值沿用零写入"
    assert caller.calls[0][1]["args"]["temperature"] == 0.3, "参数以快照为准"


# ─────────────────── 显式改名映射（留痕切换）───────────────────


async def test_alias_mapping_switches_inflight_run_and_records_trace(monkeypatch: Any) -> None:
    """显式 aliases 映射命中 → 在飞 run 按映射显式切换并留痕。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    _merge(state, await plugin.execute(_make_ctx(state)))

    # 热重载：model-a 改名 model-b，并声明显式别名映射
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
            "aliases": {"model-a": "model-b"},
        },
    )
    second = await plugin.execute(_make_ctx(state))

    assert caller.calls[1][1]["args"]["model"] == "model-b"
    binding = second["llm_model_binding"]
    assert binding["model_id"] == "model-b"
    assert binding["migrated_from"] == "model-a"
    assert binding["migration_source"] == "aliases"
    # 迁移留痕独立成键（可观测面）
    assert second["llm_model_migration"]["from"] == "model-a"
    assert second["llm_model_migration"]["to"] == "model-b"


async def test_superseded_by_tombstone_switches_inflight_run(monkeypatch: Any) -> None:
    """墓碑声明（models.<id>.superseded_by）命中 → 在飞 run 按映射显式切换。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a")},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    _merge(state, await plugin.execute(_make_ctx(state)))

    _set_llm_config(
        monkeypatch,
        {
            "models": {
                "model-a": _model_entry("model-a", superseded_by="model-b"),
                "model-b": _model_entry("model-b"),
            },
            "defaults": {"chat": "model-b"},
        },
    )
    second = await plugin.execute(_make_ctx(state))

    assert caller.calls[1][1]["args"]["model"] == "model-b"
    assert second["llm_model_binding"]["migration_source"] == "superseded_by"


async def test_alias_mapping_not_silently_applied_to_unrelated_model(monkeypatch: Any) -> None:
    """别名映射只对命中的旧名生效：无关模型名不迁移（不静默换模型）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a"), "model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
            "aliases": {"model-a": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_state(model_id="model-b")))

    assert caller.calls[0][1]["args"]["model"] == "model-b"
    assert "migrated_from" not in result["llm_model_binding"]


# ─────────────────── fail-closed（映射未命中）───────────────────


async def test_new_run_missing_model_without_mapping_fails_closed(monkeypatch: Any) -> None:
    """模型不在可用列表且无显式映射 → 仍 fail-closed（不静默回落），错误带标记。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    with pytest.raises(RuntimeError) as excinfo:
        await plugin.execute(_make_ctx(_state(model_id="ghost-model")))

    assert MODEL_UNAVAILABLE_MARKER in str(excinfo.value)
    assert "ghost-model" in str(excinfo.value)
    assert caller.calls == [], "未命中模型不得以其他模型发起 LLM 调用"


async def test_alias_mapping_rescues_new_run_resolution(monkeypatch: Any) -> None:
    """新 run 解析未命中但显式映射命中 → 按映射解析成功（不留痕为迁移）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
            "aliases": {"old-chat": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_state(model_id="old-chat")))

    assert caller.calls[0][1]["args"]["model"] == "model-b"
    assert result["llm_model_binding"]["model_id"] == "model-b"


# ─────────────────── 防御分支（配置面不完整 / 映射病态）───────────────────


async def test_model_without_api_base_fails_closed(monkeypatch: Any) -> None:
    """模型条目缺 api_base → 显式失败（不得带病切换或兜底旧端点）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": {"provider": "prov", "model_name": "model-a"}},
            "defaults": {"chat": "model-a"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    with pytest.raises(RuntimeError, match="api_base"):
        await plugin.execute(_make_ctx(_state()))

    assert caller.calls == []


async def test_binding_with_invalid_model_id_treated_as_absent(monkeypatch: Any) -> None:
    """绑定快照形态非法（model_id 非字符串/空串）→ 视为无绑定，按当前配置重解析。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    broken = {"run_id": "run-1", "model_id": "", "provider": "prov", "model_name": "model-a"}

    result = await plugin.execute(_make_ctx(_state(llm_model_binding=broken)))

    assert caller.calls[0][1]["args"]["model"] == "model-b"
    assert result["llm_model_binding"]["model_id"] == "model-b"


async def test_alias_chain_longer_than_hop_limit_fails_closed(monkeypatch: Any) -> None:
    """映射链超跳数上限（病态配置）→ 终止解析并 fail-closed（不死循环）。"""
    aliases = {f"model-{i}": f"model-{i + 1}" for i in range(8)}
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-9": _model_entry("model-9")},
            "defaults": {"chat": "model-0"},
            "aliases": aliases,
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    with pytest.raises(RuntimeError) as excinfo:
        await plugin.execute(_make_ctx(_state(model_id="model-0")))

    assert MODEL_UNAVAILABLE_MARKER in str(excinfo.value)
    assert caller.calls == []


async def test_alias_lookup_is_case_insensitive(monkeypatch: Any) -> None:
    """别名表按大小写不敏感查（与 models 查表口径一致）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
            "aliases": {"MODEL-A": "model-b"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_state(model_id="model-a")))

    assert caller.calls[0][1]["args"]["model"] == "model-b"
    assert result["llm_model_binding"]["model_id"] == "model-b"


async def test_partial_stream_result_carries_binding_and_migration(monkeypatch: Any) -> None:
    """流中断半截落库路径同样携带绑定快照与迁移留痕（不留即下轮丢绑定）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-a": _model_entry("model-a")},
            "defaults": {"chat": "model-a"},
        },
    )
    partial = {
        "success": True,
        "data": {
            "status": "error",
            "partial": {"text": "半截", "tool_calls": [], "usage": {}},
        },
    }
    caller = _FakeCaller(partial)
    plugin = _make_plugin(caller)
    state = _state()
    _merge(state, await plugin.execute(_make_ctx(state)))

    _set_llm_config(
        monkeypatch,
        {
            "models": {"model-b": _model_entry("model-b")},
            "defaults": {"chat": "model-b"},
            "aliases": {"model-a": "model-b"},
        },
    )
    second = await plugin.execute(_make_ctx(state))

    assert second["llm_model_binding"]["model_id"] == "model-b"
    assert second["llm_model_migration"]["to"] == "model-b"


async def test_state_exports_model_id_alongside_model_name(monkeypatch: Any) -> None:
    """state 同时暴露 llm_model_id（唯一标识）与 llm_model（上游模型名）。

    模型 id 是全系统唯一标识（路由/查表/凭证/超时以它为基础），下游（如
    上下文压缩链路）必须按 id 选模型——2026-10-28 装机实证：下游拿
    model_name 当 id 用，配置键改名后查不到 provider，压缩整链失效。
    """
    _set_llm_config(
        monkeypatch,
        {
            "models": {
                "gw-glm-5.3-flash": _model_entry(
                    "gw-glm-5.3-flash", model_name="glm-5.3-flash"
                )
            },
            "defaults": {"chat": "gw-glm-5.3-flash"},
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    result = await plugin.execute(_make_ctx(state))

    assert result["llm_model_id"] == "gw-glm-5.3-flash"
    assert result["llm_model"] == "glm-5.3-flash"
    # 调用面发往 llm_service 的 model 也是 id（既有契约不变）
    assert caller.calls[0][1]["args"]["model"] == "gw-glm-5.3-flash"


async def test_capability_timeout_resolves_by_model_id(monkeypatch: Any) -> None:
    """能力调用超时按 model id 查模型条目（model_name 形态查不到 → 静默回落）。"""
    _set_llm_config(
        monkeypatch,
        {
            "models": {
                "gw-glm-5.3-flash": _model_entry(
                    "gw-glm-5.3-flash",
                    model_name="glm-5.3-flash",
                    call_timeout=1234,
                )
            },
            "defaults": {"chat": "gw-glm-5.3-flash"},
        },
    )
    timeouts: list[float | None] = []

    class _TimeoutCapturingCaller(_FakeCaller):
        async def __call__(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            timeouts.append(timeout)
            return await super().__call__(method, params, timeout)

    caller = _TimeoutCapturingCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = _state()
    await plugin.execute(_make_ctx(state))

    # 超时 = 模型 call_timeout + 60s 余量（按 model id 命中条目；
    # 若按 model_name 查 → 查不到 → 静默回落 300s 口径）
    assert timeouts
    assert timeouts[0] == pytest.approx(1234 + 60)
