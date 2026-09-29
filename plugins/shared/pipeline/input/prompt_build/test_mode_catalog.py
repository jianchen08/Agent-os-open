# @feature: FP-0.2.四 模式体系 {{mode_catalog}} 目录占位符 | @ci: python-coverage
"""{{mode_catalog}} 占位符行为测试（唯一模式知识注入面，设计 D10 2026-09-28）。

契约：
1. 注册：无参类型化占位符，{{persona:}} 同体系（{{mode_catalog}} 不走
   "未识别占位符"告警路径）；
2. 两态渲染：无 mode → 全模式目录；有 mode X → 同目录 + 顶部当前模式
   标记（主 agent 编排聚焦）；条目 = mode 键 + name + description + 路由
   （pipelines 按消费情境分组 + chain.entry 入口执行者），按 mode 键排序
   确定性渲染（缓存前缀稳定依赖此）；
3. 降级：取数通道未接线 / 调用失败 / 信封无效 / 目录为空 → 空串 +
   warning（与模式物料降级同口径，提示词构建不阻断）；
4. server 接线：fetch_mode_catalog 经 tool-executor 显式 plugin_id 调
   agent_manager 的 mode.list（registry 单源，设计 D5）；单例接线可重建。

取数通道经构造参数注入（CatalogFetcher）；mock 仅限该外部服务依赖。
渲染区分度输入 ≥2：两组条目集（含/不含 description 与 chain.entry）互异，
输出随之不同；排序稳定性另有性质断言（乱序输入 → 同一输出）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402

_MODULE_NAME = "prompt_build_plugin_mode_catalog_test"


def _load_plugin_module() -> Any:
    if _MODULE_NAME in sys.modules:
        del sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_fetcher(payload: Any) -> Any:
    async def _fetch() -> Any:
        return payload

    return _fetch


def _plugin(payload: Any | None = ..., error: Exception | None = None) -> Any:
    """构造带取数通道的插件；payload=...（默认）且无 error = 通道未接线。"""
    mod = _load_plugin_module()
    if error is not None:
        async def _boom() -> Any:
            raise error

        return mod.PromptBuildPlugin(catalog_fetcher=_boom)
    if payload is ...:
        return mod.PromptBuildPlugin()
    return mod.PromptBuildPlugin(catalog_fetcher=_fake_fetcher(payload))


# 两组有区分度的目录条目（A：全字段；B：缺 description / 缺 chain.entry /
# 缺 pipelines——缺省路由不啰嗦）；A 组覆盖三种管道形态：零声明 / 仅任务链 /
# 对话链+任务链多管
_ENTRIES_A = [
    {"mode": "writing", "name": "写作模式", "description": "小说续写/扩写/改写",
     "pipelines": [], "chain": {"entry": "main"}},
    {"mode": "coding", "name": "编码模式", "description": "代码实现/修复/审查",
     "pipelines": [{"name": "coding_task", "context": "task"}], "chain": {"entry": "main"}},
    {"mode": "roleplay", "name": "角色扮演模式", "description": "沉浸式扮演对话",
     "pipelines": [{"name": "roleplay", "context": "conversation"},
                   {"name": "roleplay_tasks", "context": "task"}],
     "chain": {"entry": "main"}},
]
_ENTRIES_B = [
    {"mode": "planning", "name": "计划模式", "description": None,
     "pipelines": [], "chain": {"entry": None}},
]


# ── 1. 注册（{{persona:}} 同体系的无参类型化占位符） ──────────────────────


def test_parse_placeholder_registers_mode_catalog() -> None:
    mod = _load_plugin_module()
    var_type, params = mod.PromptBuildPlugin._parse_placeholder("mode_catalog")
    assert (var_type, params) == ("mode_catalog", {})


def test_unknown_placeholder_still_warns_distinct_from_mode_catalog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未识别占位符仍走告警路径（mode_catalog 注册不吞其它占位符）。"""
    plugin = _plugin()
    ctx = PluginContext(state={})
    with caplog.at_level("WARNING"):
        out = _run(plugin._resolve_placeholder(ctx, "mode_katalog"))
    assert out == ""
    assert any("未识别的占位符" in r.getMessage() for r in caplog.records)


# ── 2. 两态渲染 ──────────────────────────────────────────────────────────


def test_no_mode_renders_full_catalog_without_marker() -> None:
    """无 mode（默认会话）→ 全模式目录：条目按 mode 键排序、无当前模式标记。"""
    plugin = _plugin({"modes": list(reversed(_ENTRIES_A))})
    out = _run(plugin._resolve_placeholder(PluginContext(state={}), "mode_catalog"))
    lines = out.splitlines()
    assert lines[0] == "## 模式目录"
    assert not any("本会话/任务以" in ln for ln in lines), "无 mode 不得有当前模式标记"
    entry_lines = [ln for ln in lines if ln.startswith("- ")]
    assert [ln.split(" | ")[0] for ln in entry_lines] == ["- coding", "- roleplay", "- writing"]
    assert "- coding | 编码模式 | 代码实现/修复/审查 | 路由: 任务链=coding_task，入口执行者=main" in entry_lines
    assert "- roleplay | 角色扮演模式 | 沉浸式扮演对话 | 路由: 对话链=roleplay，任务链=roleplay_tasks，入口执行者=main" in entry_lines
    assert "- writing | 写作模式 | 小说续写/扩写/改写 | 路由: pipeline=autonomous，入口执行者=main" in entry_lines, "零声明 = 缺省共享管道不啰嗦"


def test_with_mode_marks_current_mode_on_top() -> None:
    """有 mode X → 同目录 + 顶部一行当前模式标记（主 agent 编排聚焦）。"""
    plugin = _plugin({"modes": _ENTRIES_A})
    ctx = PluginContext(state={"execution_context": {"mode": "coding"}})
    out = _run(plugin._resolve_placeholder(ctx, "mode_catalog"))
    lines = out.splitlines()
    assert lines[0] == "## 模式目录"
    assert lines[1] == "本会话/任务以 coding 模式执行，按该模式条目路由编排。"
    # 目录本体与无 mode 态一致（两态各自稳定，仅差标记行）
    plugin2 = _plugin({"modes": _ENTRIES_A})
    out2 = _run(plugin2._resolve_placeholder(PluginContext(state={}), "mode_catalog"))
    assert out.replace(lines[1] + "\n", "", 1) == out2


def test_catalog_render_is_deterministic_under_input_order() -> None:
    """性质断言：条目乱序输入 → 输出逐字节一致（确定性渲染，缓存前缀稳定依赖）。"""
    mod = _load_plugin_module()
    a = mod._render_mode_catalog(list(_ENTRIES_A), "")
    b = mod._render_mode_catalog(list(reversed(_ENTRIES_A)), "")
    assert a == b
    # 幂等：再渲染一次仍同值
    assert mod._render_mode_catalog(_ENTRIES_A, "") == a


def test_two_entry_sets_render_distinct_outputs() -> None:
    """区分度第二组：缺 description/chain.entry/pipelines 的条目集渲染省略
    对应段，与全字段条目集输出互异。"""
    mod = _load_plugin_module()
    out_a = mod._render_mode_catalog(_ENTRIES_A, "")
    out_b = mod._render_mode_catalog(_ENTRIES_B, "")
    assert "- planning | 计划模式 | 路由: pipeline=autonomous" in out_b
    assert "入口执行者" not in out_b and "计划模式" not in out_a
    assert out_a != out_b


def test_pipeline_route_groups_by_context_in_fixed_order() -> None:
    """多管路由按消费情境固定序渲染（对话链→任务链），与声明序无关——
    同一模式不同声明序输出一致（缓存前缀稳定依赖此）。"""
    mod = _load_plugin_module()
    forward = {"mode": "m", "name": "M", "pipelines": [
        {"name": "conv_pipe", "context": "conversation"},
        {"name": "task_pipe", "context": "task"},
    ]}
    reversed_decl = {"mode": "m", "name": "M", "pipelines": [
        {"name": "task_pipe", "context": "task"},
        {"name": "conv_pipe", "context": "conversation"},
    ]}
    a = mod._render_pipeline_route(forward)
    b = mod._render_pipeline_route(reversed_decl)
    assert a == b == "路由: 对话链=conv_pipe，任务链=task_pipe"


def test_pipeline_route_single_context_renders_only_that_segment() -> None:
    """仅任务链声明 → 只渲染任务链段（无对话链占位）；零声明 → 缺省
    autonomous（不啰嗦）——两组区分度输入。"""
    mod = _load_plugin_module()
    assert mod._render_pipeline_route({
        "pipelines": [{"name": "t", "context": "task"}], "chain": {"entry": None},
    }) == "路由: 任务链=t"
    assert mod._render_pipeline_route({"pipelines": [], "chain": {"entry": None}}) == "路由: pipeline=autonomous"


def test_render_omits_absent_segments() -> None:
    """description=None / chain.entry=None 的段自然省略（不渲染占位符）。"""
    mod = _load_plugin_module()
    out = mod._render_mode_catalog(_ENTRIES_B, "")
    assert "None" not in out and "|" in out


def test_bad_entries_filtered_in_envelope_unwrap() -> None:
    """信封内坏条目（非 dict / 无 mode 键 / 空 mode）剔除，好条目保留。"""
    mod = _load_plugin_module()
    good = dict(_ENTRIES_A[0])
    modes = [good, {"no_mode": 1}, {"mode": ""}, {"mode": 42}, "junk"]
    entries = mod._unwrap_mode_catalog({"modes": modes})
    assert [e["mode"] for e in entries] == [good["mode"]]


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": 1},
        {"data": {"not_modes": []}},
        {"modes": "not-a-list"},
        "garbage",
    ],
)
def test_invalid_envelope_raises_for_degradation(payload: Any) -> None:
    """无效信封形态 → ValueError（消费方降级空串）。"""
    mod = _load_plugin_module()
    with pytest.raises(ValueError, match="无效目录"):
        mod._unwrap_mode_catalog(payload)


def test_envelope_both_shapes_tolerated() -> None:
    """两信封形态（本体 / {"data": {...}}）都解析出同一条目集。"""
    mod = _load_plugin_module()
    direct = mod._unwrap_mode_catalog({"modes": _ENTRIES_A})
    wrapped = mod._unwrap_mode_catalog({"data": {"modes": _ENTRIES_A}})
    assert direct == wrapped == _ENTRIES_A


# ── 3. 降级（空串 + warning，不阻断） ────────────────────────────────────


def test_unwired_fetcher_degrades_to_empty(caplog: pytest.LogCaptureFixture) -> None:
    """取数通道未接线（构造未注入）→ 空串 + warning。"""
    plugin = _plugin()
    with caplog.at_level("WARNING"):
        out = _run(plugin._resolve_placeholder(PluginContext(state={}), "mode_catalog"))
    assert out == ""
    assert any("取数通道未接线" in r.getMessage() for r in caplog.records)


def test_fetcher_failure_degrades_to_empty(caplog: pytest.LogCaptureFixture) -> None:
    """mode.list 调用抛错 → 空串 + warning（服务不可用不阻断提示词构建）。"""
    plugin = _plugin(error=RuntimeError("agent_manager sidecar down"))
    with caplog.at_level("WARNING"):
        out = _run(plugin._resolve_placeholder(PluginContext(state={}), "mode_catalog"))
    assert out == ""
    assert any("取数失败" in r.getMessage() for r in caplog.records)


def test_invalid_envelope_degrades_to_empty(caplog: pytest.LogCaptureFixture) -> None:
    """服务返回无效信封 → ValueError 路径 → 空串 + warning。"""
    plugin = _plugin({"unexpected": 1})
    with caplog.at_level("WARNING"):
        out = _run(plugin._resolve_placeholder(PluginContext(state={}), "mode_catalog"))
    assert out == ""
    assert any("取数失败" in r.getMessage() for r in caplog.records)


def test_empty_catalog_renders_empty() -> None:
    """目录为空（无模式包的合法形态）→ 空串零注入（无孤头标）。"""
    plugin = _plugin({"modes": [], "total": 0})
    out = _run(plugin._resolve_placeholder(PluginContext(state={}), "mode_catalog"))
    assert out == ""


def test_degradation_keeps_prompt_build_unblocked() -> None:
    """端到端降级：取数失败时 system_prompt 渲染照常完成（占位符清空）。"""
    plugin = _plugin(error=RuntimeError("down"))
    ctx = PluginContext(
        state={"context.system_prompt": "# 骨架\n\n{{mode_catalog}}\n\n## 流程\n做事。"}
    )
    out = _run(plugin._resolve_placeholders(ctx, ctx.state["context.system_prompt"]))
    assert out == "# 骨架\n\n\n\n## 流程\n做事。"


# ── 4. 端到端（system_prompt 模板内整体替换） ────────────────────────────


def test_mode_catalog_in_system_prompt_end_to_end() -> None:
    """模板 {{mode_catalog}} 整体替换为目录文本，骨架其余部分原样保留。"""
    plugin = _plugin({"modes": _ENTRIES_A})
    ctx = PluginContext(
        state={"context.system_prompt": "# 你是灵汐\n\n{{mode_catalog}}\n\n## 流程\n做事。"}
    )
    out = _run(plugin._resolve_placeholders(ctx, ctx.state["context.system_prompt"]))
    assert out.startswith("# 你是灵汐\n\n## 模式目录\n")
    assert out.endswith("\n- writing | 写作模式 | 小说续写/扩写/改写 | 路由: pipeline=autonomous，入口执行者=main\n\n## 流程\n做事。")
    assert "{{mode_catalog}}" not in out


def test_mode_catalog_with_mode_marker_end_to_end() -> None:
    """有 mode 端到端：标记行紧随目录头标，目录条目仍全量在场。"""
    plugin = _plugin({"modes": _ENTRIES_A})
    ctx = PluginContext(
        state={
            "execution_context": {"mode": "roleplay"},
            "context.system_prompt": "{{mode_catalog}}",
        }
    )
    out = _run(plugin._resolve_placeholders(ctx, ctx.state["context.system_prompt"]))
    assert "本会话/任务以 roleplay 模式执行" in out
    assert "- coding |" in out and "- writing |" in out, "目录不因标记收窄"


# ── 5. server 接缝（mode.list 取数通道接线） ─────────────────────────────


def _load_server_module() -> Any:
    sys.modules.pop("plugin", None)
    mod_name = "prompt_build_server_mode_catalog_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class FakeCapabilityHandle:
    """tool-executor 句柄替身：记录 call(method, params)，回放脚本化返回。"""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        return self.result


class TestServerSeam:
    def test_fetch_mode_catalog_invoke_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """调用形状钉死：invoke + 显式 plugin_id=agent_manager + tool_name=mode.list；
        信封原样透传（解析归插件侧 _unwrap_mode_catalog）。"""
        server = _load_server_module()
        envelope = {"data": {"modes": [], "total": 0}}
        handle = FakeCapabilityHandle(result=envelope)
        monkeypatch.setattr(server.plugin, "get_capability", lambda _name: handle)

        res = _run(server.fetch_mode_catalog())

        assert res == envelope, "信封原样透传"
        assert handle.calls == [
            ("invoke", {"tool_name": "mode.list", "plugin_id": "agent_manager", "args": {}})
        ], "tool-executor 显式 plugin_id 通道（agent_manager）"

    def test_capability_absent_raises_key_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tool-executor 未注入 → KeyError 上抛（降级裁决在插件侧）。"""
        server = _load_server_module()

        def _raise(name: str) -> Any:
            raise KeyError(f"capability not injected: {name}")

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        with pytest.raises(KeyError):
            _run(server.fetch_mode_catalog())

    def test_singleton_wires_catalog_fetcher_and_rebuilds(self) -> None:
        """单例带 fetch_mode_catalog 接线；on_unload 复位缓存后重建新实例。"""
        server = _load_server_module()
        inst = server.get_instance()
        assert inst._catalog_fetcher is server.fetch_mode_catalog, (
            "get_instance 必须把 mode.list 取数通道接进插件"
        )
        assert server.get_instance() is inst, "lru_cache 幂等"

        _run(server._on_unload({}))
        second = server.get_instance()
        assert second is not inst, "on_unload 复位后应重建单例"
        assert second._catalog_fetcher is server.fetch_mode_catalog
