# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""param_inject 分支补测（与既有 test_param_inject.py 互补，锁定车道缺口分支契约）：

- classify_args_parse_failure 五类诚实分类（empty/markdown_wrapped/leading_noise/
  truncated/malformed），含字符串状态机（转义对消费不翻转字符串态、括号配对计数）；
- JSON 修复信封契约：成功信封取 data.repaired（data 非 dict / repaired 非 str →
  None）；失败信封 / 非 dict 信封 / 能力调用异常 → None（降级不阻塞注入）；
- arguments 修复成功且可解析 → 保住字段；仅真实分类为 truncated 才打
  ``_args_truncated`` 标记；修复产物仍不可解析 → args 置空 dict；
- arguments 解析结果非 dict（数组/字符串/数字）→ 整体丢弃，args 仅剩服务端注入；
- priority 可配置（缺省 20）；LLM 自带 parent_agent_level / agent_config_id
  → 早退保留，不被服务端值覆盖；
- default_params 对注入后仍缺位的键补注；未登记工具不套用；
- {{project_root}} 展开：锚点解析失败整值保持字面量；成功仅替换含模板的字符串值。

capability caller / 仓库锚点解析是外部依赖（内核能力面 / env+文件系统），以伪对象
或 monkeypatch 注入；不 mock 插件内部协作对象。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[2]  # plugins/shared（pipeline 包）
for _p in (str(_SHARED_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD_NAME = "param_inject_plugin_gaps_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return module


mod = _load_module()
ParamInjectPlugin = mod.ParamInjectPlugin

# 完整 _state 下服务端注入 keys（非 dict arguments 丢弃后 args 应恰好只剩这些）
_INJECTED_KEYS = {
    "session_id",
    "user_id",
    "timestamp",
    "task_id",
    "pipeline_id",
    "parent_agent_level",
    "agent_config_id",
    "workspace",
    "isolation_level",
    "project_root",
}


def _make_plugin(config: dict | None = None) -> Any:
    return ParamInjectPlugin(config=config)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": "memory", "args": {}}],
        "session_id": "s-1",
        "user_id": "u-1",
        "task.id": "t-1",
        "pipeline_id": "t-1",
        "agent_level": "L2",
        "agent_config_id": "cfg-9",
        "workspace": "/ws/root",
        "isolation_level": "isolated",
        "project_root": "/pr/root",
    }
    state.update(overrides)
    return state


def _ctx(state: dict[str, Any]) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=state)


def _calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["raw_tool_calls"]


async def _run(state: dict[str, Any], config: dict | None = None) -> dict[str, Any]:
    return await _make_plugin(config)._do_work(_ctx(state))


@pytest.fixture
def install_capability_caller() -> Any:
    """安装伪能力调用器（外部依赖：内核能力面），用后复位为未注入态。"""

    def _install(caller: Any) -> None:
        mod.set_capability_caller(caller)

    yield _install
    mod.set_capability_caller(None)


def _caller_returning(envelope: Any) -> Any:
    async def _caller(method: str, params: dict[str, Any], timeout: float | None) -> Any:
        return envelope

    return _caller


def _caller_raising(exc: Exception) -> Any:
    async def _caller(method: str, params: dict[str, Any], timeout: float | None) -> Any:
        raise exc

    return _caller


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "empty"),
        ("   \n\t  ", "empty"),
        ('```json\n{"a": 1}', "markdown_wrapped"),
        ("好的，参数如下：{...}", "leading_noise"),
        ("Error: before json {", "leading_noise"),
        ('{"a": "v\\", ', "truncated"),  # \" 是转义对：不翻转字符串态，{ 未闭合
        ('{"a": [1, 2', "truncated"),  # 无转义的朴素截断对照
        ('{"a": 1, "b": 2}', "malformed"),  # 括号配对完整，失败属语法错误
        ('{"a": x, "b": }', "malformed"),
    ],
)
def test_classify_args_parse_failure_categories(raw: str, expected: str) -> None:
    """解析失败原因诚实分类：空/Markdown 包裹/前导噪声/结构截断/语法错误。"""
    assert mod.classify_args_parse_failure(raw) == expected


@pytest.mark.parametrize(
    ("caller_factory", "expected"),
    [
        (lambda: _caller_returning({"success": True, "data": "not-a-dict"}), None),
        (lambda: _caller_returning({"success": True, "data": {"repaired": '{"a": 1}'}}), '{"a": 1}'),
        (lambda: _caller_returning({"success": True, "data": {"repaired": 7}}), None),
        (lambda: _caller_returning({"success": False, "data": {"repaired": "x"}}), None),
        (lambda: _caller_returning("totally-not-an-envelope"), None),
        (lambda: _caller_raising(RuntimeError("capability down")), None),
    ],
)
async def test_repair_json_string_envelope_contract(
    install_capability_caller: Any, caller_factory: Any, expected: str | None
) -> None:
    """修复信封契约：仅 success 信封 + dict data + str repaired 才返回修复串。"""
    install_capability_caller(caller_factory())
    assert await mod._repair_json_string('{"a":') == expected


@pytest.mark.parametrize(
    ("raw_arguments", "repaired", "expect_truncated_marker", "expect_content"),
    [
        ('{"content": "hello', '{"content": "hello world"}', True, "hello world"),
        ('```json\n{"content": "hi"}', '{"content": "hi"}', False, "hi"),
        ('{"content": "hello', "still-not-json", True, None),
    ],
    ids=["truncated-repaired", "markdown-repaired-no-marker", "truncated-repair-unparseable"],
)
async def test_arguments_repair_end_to_end(
    install_capability_caller: Any,
    raw_arguments: str,
    repaired: str,
    expect_truncated_marker: bool,
    expect_content: str | None,
) -> None:
    """兜底修复端到端：可解析保字段；仅 truncated 打标；修复产物不可解析 → 空 args。"""
    install_capability_caller(
        _caller_returning({"success": True, "data": {"repaired": repaired}})
    )
    state = _state(raw_tool_calls=[{"name": "memory", "arguments": raw_arguments}])
    tc = _calls(await _run(state))[0]
    args = tc["args"]
    assert tc.get("_args_truncated", False) is expect_truncated_marker
    if expect_content is None:
        assert "content" not in args  # 修复产物不可解析 → 字段不保（置空 dict）
    else:
        assert args["content"] == expect_content
    assert args["session_id"] == "s-1"  # 修复成功/降级均不阻塞后续身份注入


@pytest.mark.parametrize(
    "raw_arguments",
    ['["not", "a", "dict"]', '"bare string"', "42"],
    ids=["json-array", "json-string", "json-number"],
)
async def test_arguments_parsed_to_non_dict_yields_injection_only_args(raw_arguments: str) -> None:
    """arguments 解析为非 dict → 整体丢弃，args 恰好只剩服务端注入键。"""
    result = await _run(_state(raw_tool_calls=[{"name": "memory", "arguments": raw_arguments}]))
    args = _calls(result)[0]["args"]
    assert set(args) == _INJECTED_KEYS


def test_priority_configurable_with_default() -> None:
    """priority 取自配置，缺省 20；插件名恒为 param_inject。"""
    assert _make_plugin(config={"priority": 7}).priority == 7
    assert _make_plugin().priority == 20
    assert _make_plugin().name == "param_inject"


@pytest.mark.parametrize(
    ("llm_key", "llm_value", "state_key", "state_value"),
    [
        ("parent_agent_level", 3, "agent_level", "L2"),
        ("agent_config_id", "cfg-llm", "agent_config_id", "cfg-9"),
    ],
    ids=["parent-agent-level", "agent-config-id"],
)
async def test_llm_supplied_keys_taken_as_is(
    llm_key: str, llm_value: Any, state_key: str, state_value: Any
) -> None:
    """LLM 已带 parent_agent_level / agent_config_id → 早退保留，服务端值不覆盖。"""
    state = _state(
        **{state_key: state_value},
        raw_tool_calls=[{"name": "memory", "args": {llm_key: llm_value}}],
    )
    args = _calls(await _run(state))[0]["args"]
    assert args[llm_key] == llm_value


async def test_default_params_fill_key_absent_after_injections() -> None:
    """default_params 补注注入后仍缺位的键；已有值不覆盖。"""
    config = {"default_params": {"memory": {"top_k": 5, "scope": "task"}}}
    state = _state(raw_tool_calls=[{"name": "memory", "args": {"scope": "global"}}])
    args = _calls(await _run(state, config))[0]["args"]
    assert args["top_k"] == 5
    assert args["scope"] == "global"


async def test_default_params_skipped_for_unlisted_tool() -> None:
    """未登记 default_params 的工具不套用默认参数。"""
    config = {"default_params": {"memory": {"top_k": 5}}}
    state = _state(raw_tool_calls=[{"name": "bash_execute", "args": {}}])
    args = _calls(await _run(state, config))[0]["args"]
    assert "top_k" not in args


async def test_project_root_template_replaces_only_template_strings(monkeypatch: Any) -> None:
    """锚点解析成功：仅替换含 {{project_root}} 的字符串值，其它键值不动。

    仓库锚点解析依赖 env + 文件系统（外部环境），monkeypatch 固定解析结果，
    使断言与环境无关。
    """
    anchor = Path("/fake/repo")
    monkeypatch.setattr(mod, "_resolve_project_root", lambda: anchor)
    state = _state(
        raw_tool_calls=[
            {
                "name": "memory",
                "args": {
                    "path": "{{project_root}}/config",
                    "label": "no {{other}} template",
                    "depth": 3,
                },
            }
        ]
    )
    args = _calls(await _run(state))[0]["args"]
    assert args["path"] == f"{anchor}/config"  # 替换值为锚点的 str 形态（OS 原生）
    assert args["label"] == "no {{other}} template"
    assert args["depth"] == 3


async def test_execute_wraps_do_work_in_plugin_result() -> None:
    """公共入口 execute：_do_work 结果包进 PluginResult.state_updates。

    断信封形状而非 isinstance：车道共跑下 pipeline 包存在双实例，
    import 身份判定会假阴（生产类与测试面各自绑定一份 facade 重导出）。
    """
    ctx = _ctx(_state(raw_tool_calls=[{"name": "memory", "args": {}}]))
    result = await _make_plugin().execute(ctx)
    assert type(result).__name__ == "PluginResult"
    assert result.error is None
    assert result.skip_remaining is False
    assert result.state_updates["tool.params_injected"] is True
    assert _calls(result.state_updates)[0]["args"]["session_id"] == "s-1"


async def test_project_root_unresolvable_keeps_literal(monkeypatch: Any) -> None:
    """锚点解析失败（None）：模板串保持字面量，不做替换。"""
    monkeypatch.setattr(mod, "_resolve_project_root", lambda: None)
    state = _state(raw_tool_calls=[{"name": "memory", "args": {"path": "{{project_root}}/config"}}])
    args = _calls(await _run(state))[0]["args"]
    assert args["path"] == "{{project_root}}/config"
