# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core/plugin.py 缺口补测（官方车道 coverage.xml 2026-09-13 miss=38）。

覆盖面（按行为分组）：
- 模块级共享根自举（sys.path bootstrap 真实执行）与 priority 元数据；
- 模型解析：state.model_tier → defaults.tiers（_resolve_tier）、model_id 大小写
  不敏感命中 / 缺失保持当前配置；
- 空回复重试策略：plugin_configs 覆盖 max_empty_retries、跨轮计数、耗尽裁决；
- assistant 消息组装：tool_calls + reasoning_content 保留、空回复不追加消息；
- 多模态：list 形态 content 直接追加、本地引用解析失败（文件不存在/读取失败）；
- 请求装配：multimodal 合并、prompt.dynamic_vars 追加、tool_schemas 透传、
  不可序列化 tool_calls 不炸调用；
- 能力信封 fail-closed：非 dict 信封 / 失败信封 / 非 dict data 三种形状异常。

LLM 调用面（tool-executor capability → llm_service）按外部依赖 mock；
模型配置经 _config_models.set_config 公共注入口注入；管道 state 用真实 dict。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core"
_LLM_CORE_DIR = _CORE_DIR / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"

# sys.path 自举：先把共享根摘出，让 plugin.py 顶部的共享根自举行
# （_SHARED_ROOT not in sys.path → insert(0)）真实执行；再保证 llm_core 包目录
# 与 system/llm（adapter / _config_models 平铺模块落点）在最前（车道共跑时
# 其他插件目录可能残留 sys.path 前部，幂等跳过会让 `from adapter import`
# 命中他插件）。
while str(_SHARED_DIR) in sys.path:
    sys.path.remove(str(_SHARED_DIR))
for _d in (_SYSTEM_LLM_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))


def _load_plugin_module():
    """唯一名动态加载 plugin.py（裸名 plugin 会被兄弟插件目录串扰）。"""
    for stale in list(sys.modules):
        if stale == "llm_core" or stale.startswith("llm_core."):
            sys.modules.pop(stale, None)
    name = "_llm_core_gaps_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin_mod = _load_plugin_module()
# 加载期 `from adapter import`（plugin.py 顶部平铺导入）会把解析命中的
# adapter 裸名缓存进 sys.modules；本文件之后收集的裸名导入方（如
# test_llm_adapter_call_streaming 的 `import adapter`）会命中这份缓存而非
# 自己 sys.path 位的同名模块。自身引用在加载期已绑定，逐出不伤本文件。
sys.modules.pop("adapter", None)
LLMCore = plugin_mod.LLMCore


@pytest.fixture(autouse=True)
def _isolate_llm_config_and_caller():
    """隔离模型配置注入与能力调用句柄（退出时恢复现场）。"""
    from _config_models import get_config, set_config

    prev = get_config()
    yield
    set_config(prev)
    plugin_mod.set_capability_caller(None)


class _Ctx:
    """最小插件执行上下文（state 为真实 dict）。"""

    def __init__(self, state: dict[str, Any]):
        self.state = state


class _RecordingCaller:
    """伪 capability caller：记录 (method, params, timeout) 并返回预设信封。"""

    def __init__(self, reply: Any):
        self.reply = reply
        self.calls: list[tuple[str, dict, float | None]] = []

    async def __call__(self, method: str, params: dict, timeout: float | None = None):
        self.calls.append((method, params, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _llm_data(**over: Any) -> dict[str, Any]:
    """tool-executor.invoke 成功信封（llm.complete_stream 聚合响应形态）。"""
    data: dict[str, Any] = {
        "text": "ok",
        "tool_calls": [],
        "thinking_text": None,
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        "finish_reason": "stop",
        "partial": None,
    }
    data.update(over)
    return {"success": True, "data": data}


def _make_plugin(caller: _RecordingCaller, config: dict[str, Any] | None = None) -> LLMCore:
    plugin_mod.set_capability_caller(caller)
    return LLMCore(config or {"provider": "openai", "model_name": "gpt-4"})


# llm.yaml 注入形态（models/providers/defaults 三段，真实 loader 消费）
_MODEL_CONFIG = {
    "llm": {
        "defaults": {
            "tiers": {"large": "big-model", "small": "small-model"},
            "chat": "big-model",
        },
        "models": {
            "big-model": {
                "provider": "prov-a",
                "model_name": "BigModel",
                "api_base": "https://a.example.com",
                "context_window": 32000,
                "default_params": {"temperature": 0.5},
            },
            "small-model": {
                "provider": "prov-b",
                "model_name": "SmallModel",
                "api_base": "https://b.example.com",
                "context_window": 8000,
            },
        },
        "providers": {"prov-a": {"api_key": "ka"}, "prov-b": {"api_key": "kb"}},
    }
}


# ── 模块自举与插件元数据 ─────────────────────────────────────────────


def test_module_bootstrap_puts_shared_root_on_sys_path_once():
    """共享根自举在缺失时真实插入且不重复（flat 模块解析依赖此前置）。"""
    occurrences = [p for p in sys.path if p == str(_SHARED_DIR)]
    assert occurrences == [str(_SHARED_DIR)]  # 恰好一处、位于可解析位置


def test_plugin_priority_metadata():
    """priority 元数据：核心 LLM 调用步固定 50。"""
    assert LLMCore({}).priority == 50


# ── 模型解析：tier 路由 + model_id 查表 ─────────────────────────────


@pytest.mark.parametrize(
    ("tier", "want_model", "want_api_base", "want_window"),
    [
        ("large", "BigModel", "https://a.example.com", 32000),
        ("small", "SmallModel", "https://b.example.com", 8000),
    ],
    ids=["tier-large", "tier-small"],
)
async def test_execute_resolves_model_via_state_tier(tier, want_model, want_api_base, want_window):
    """state.model_tier → defaults.tiers → 整套配置切换（provider/端点/窗口随模型走）。"""
    from _config_models import set_config

    set_config(_MODEL_CONFIG)
    caller = _RecordingCaller(_llm_data())
    plugin = _make_plugin(caller)

    result = await plugin.execute(
        _Ctx({"messages": [{"role": "user", "content": "hi"}], "model_tier": tier})
    )

    assert result["llm_model"] == want_model
    assert result["llm_provider"] in ("prov-a", "prov-b")
    assert result["llm_api_base"] == want_api_base
    assert result["track.model_context_window"] == want_window
    method, params, timeout = caller.calls[0]
    assert method == "invoke"
    # 请求用 model_id（yaml key）做 deployment 匹配，非展示名
    assert params["args"]["model"] == {"large": "big-model", "small": "small-model"}[tier]
    # 长等待语义：call_timeout（defaults 300s）+ 余量，SDK 默认 30s 会先于 LLM 掐断
    assert timeout > 300.0
    # 解析后模型的 default_params 合并进请求（small-model 未配置 → 不设采样参数）
    if tier == "large":
        assert params["args"]["temperature"] == 0.5
    else:
        assert "temperature" not in params["args"]


@pytest.mark.parametrize(
    ("requested", "want_model", "want_api_base"),
    [
        ("ghost-model", "FallbackKeep", "https://keep.example.com"),  # 缺配置 → 保持当前模型
        ("BIG-MODEL", "BigModel", "https://a.example.com"),  # 大小写不敏感命中
    ],
    ids=["missing-config-keeps-current", "case-insensitive-hit"],
)
async def test_execute_model_id_lookup(requested, want_model, want_api_base):
    """state.model_id 查表：未找到告警并保持原配置（不阻断）；命中含大小写不敏感。"""
    from _config_models import set_config

    set_config(_MODEL_CONFIG)
    caller = _RecordingCaller(_llm_data())
    plugin = _make_plugin(
        caller,
        {
            "provider": "openai",
            "model_name": "FallbackKeep",
            "api_base": "https://keep.example.com",
        },
    )

    result = await plugin.execute(
        _Ctx({"messages": [{"role": "user", "content": "hi"}], "model_id": requested})
    )

    assert result["llm_model"] == want_model
    assert result["llm_api_base"] == want_api_base


# ── 空回复重试策略（plugin_configs 覆盖 + 跨轮计数）──────────────────


async def test_empty_response_exhausts_when_override_zero():
    """plugin_configs 覆盖 max_empty_retries=0：首个空回复即耗尽，交任务域裁决。"""
    caller = _RecordingCaller(_llm_data(text=""))
    plugin = _make_plugin(caller, {"provider": "openai", "model_name": "m", "max_empty_retries": 5})

    result = await plugin.execute(
        _Ctx(
            {
                "messages": [],
                "pipeline_id": "p-exhaust",
                "plugin_configs": {"llm_core": {"max_empty_retries": 0}},
            }
        )
    )

    assert result["llm_empty_streak"] == 1
    assert result["llm_empty_exhausted"] is True
    assert result["_has_new_llm_input"] is False
    # 空回复（无文本无工具调用）不产出 assistant 消息
    assert "messages" not in result


async def test_empty_response_within_budget_requests_retry():
    """未超限：计数累加（prev+1）并置续跑标志回 LLM 重试。"""
    caller = _RecordingCaller(_llm_data(text=""))
    plugin = _make_plugin(caller, {"provider": "openai", "model_name": "m", "max_empty_retries": 5})

    result = await plugin.execute(
        _Ctx(
            {
                "messages": [],
                "pipeline_id": "p-retry",
                "llm_empty_streak": 1,
                "plugin_configs": {"llm_core": {"max_empty_retries": 2}},
            }
        )
    )

    assert result["llm_empty_streak"] == 2  # 跨轮计数：prev + 1
    assert "llm_empty_exhausted" not in result
    assert result["_has_new_llm_input"] is True
    assert "messages" not in result


async def test_empty_response_without_override_uses_constructor_default():
    """无 plugin_configs 覆盖 → 构造默认 max_empty_retries=2 生效（每次 execute 复位）。"""
    caller = _RecordingCaller(_llm_data(text=""))
    plugin = _make_plugin(caller, {"provider": "openai", "model_name": "m", "max_empty_retries": 1})

    result = await plugin.execute(_Ctx({"messages": [], "llm_empty_streak": 1}))

    assert result["llm_empty_exhausted"] is True  # streak 2 > 1


# ── assistant 消息组装：tool_calls + reasoning 保留 + id 标准化 ──────


async def test_tool_call_message_keeps_reasoning_and_long_args():
    """tool_calls + 思考文本 → reasoning_content 随 assistant 消息保留；
    长字符串 arguments 原样透传（诊断日志分支不改动载荷）。"""
    long_args = "x" * 150
    caller = _RecordingCaller(
        _llm_data(
            text=None,
            thinking_text="先读文件再改",
            tool_calls=[{"id": "call_" + "ab" * 12, "name": "write_file", "args": long_args}],
        )
    )
    plugin = _make_plugin(caller)

    result = await plugin.execute(_Ctx({"messages": [{"role": "user", "content": "hi"}]}))

    ops = result["messages"]["_ops"]
    assert len(ops) == 1
    msg = ops[0]["msg"]
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0]["function"]["arguments"] == long_args
    assert msg["tool_calls"][0]["function"]["name"] == "write_file"
    assert msg["reasoning_content"] == "先读文件再改"
    # state 与 assistant 消息使用同一份 id
    assert result["raw_tool_calls"][0]["id"] == msg["tool_calls"][0]["id"]


async def test_nonstandard_tool_call_ids_are_rewritten():
    """非标准 id（call_function_x_1）统一重写为 call_<hex> 标准格式。"""
    caller = _RecordingCaller(
        _llm_data(
            text=None,
            tool_calls=[
                {"id": "call_function_write_1", "name": "write_file", "args": "{}"},
                {"id": "call_" + "0123456789abcdef" * 2, "name": "read_file", "args": "{}"},
            ],
        )
    )
    plugin = _make_plugin(caller)

    result = await plugin.execute(_Ctx({"messages": []}))

    ids = [tc["id"] for tc in result["raw_tool_calls"]]
    import re

    pattern = re.compile(r"call_[0-9a-f]+\Z")
    assert all(pattern.fullmatch(tc_id) for tc_id in ids)  # 性质：全部标准化
    assert len(set(ids)) == 2  # 重写不碰撞


# ── 请求装配：multimodal / dynamic_vars / tool_schemas / 序列化兜底 ──


@pytest.mark.parametrize(
    ("content", "want_block_count"),
    [
        ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], 3),  # 已是 blocks → 追加
        ("看这张图", 2),  # 纯文本 → 包一个 text block 再追加
    ],
    ids=["content-blocks-list", "plain-string"],
)
def test_build_messages_merges_multimodal_into_last_user_message(content, want_block_count):
    """multimodal 引用块合并进最后一条 user 消息：既有内容保持前缀、图片块殿后。"""
    plugin = LLMCore({"model_name": "m"})
    image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}

    msgs = plugin._build_messages(
        {
            "messages": [{"role": "user", "content": content, "seq": 1}],
            "multimodal_content": [image_block],
        }
    )

    user = [m for m in msgs if m.get("role") == "user"][-1]
    blocks = user["content"]
    assert isinstance(blocks, list)
    assert len(blocks) == want_block_count
    assert blocks[0]["type"] == "text"  # 既有文本内容在前
    assert blocks[-1] == image_block  # 图片块追加在后


@pytest.mark.parametrize(
    ("dynamic_vars", "want_content"),
    [
        ({"content": "<dynamic_vars>time=now</dynamic_vars>"}, "<dynamic_vars>time=now</dynamic_vars>"),
        ("session_id=s1", "session_id=s1"),
        ({"content": ""}, None),  # 空 content 不追加
    ],
    ids=["dict-var", "string-var", "empty-content-not-appended"],
)
def test_build_messages_dynamic_vars_appended_as_user_message(dynamic_vars, want_content):
    """动态变量独立 user 消息追加在末尾；system 消息保持首位不变（cache 契约）。"""
    plugin = LLMCore({"model_name": "m"})
    system = {"role": "system", "content": "SYS"}

    msgs = plugin._build_messages(
        {
            "system_message": system,
            "messages": [{"role": "user", "content": "hi", "seq": 0}],
            "prompt.dynamic_vars": dynamic_vars,
        }
    )

    assert msgs[0] is system
    assert msgs[0]["content"] == "SYS"
    if want_content is None:
        assert msgs[-1]["content"] == "hi"  # 无动态变量消息
    else:
        assert msgs[-1] == {"role": "user", "content": want_content}


_SCHEMA_A = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
_SCHEMA_B = {"type": "function", "function": {"name": "write_file", "parameters": {}}}


@pytest.mark.parametrize(
    ("tool_schemas", "expect_tools"),
    [
        ([_SCHEMA_A, _SCHEMA_B], True),
        ([], False),
    ],
    ids=["with-schemas", "empty-surface"],
)
async def test_tool_schemas_passthrough_to_service_call(tool_schemas, expect_tools):
    """state.tool_schemas → kwargs.tools 透传；空面不携带该键（形参默认 None）。"""
    caller = _RecordingCaller(_llm_data())
    plugin = _make_plugin(caller)

    await plugin.execute(_Ctx({"messages": [], "tool_schemas": tool_schemas}))

    args = caller.calls[0][1]["args"]
    if expect_tools:
        assert args["tools"] == tool_schemas
    else:
        assert "tools" not in args


def _circular_tool_calls() -> list[dict]:
    tc = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    tc["cycle"] = tc  # 循环引用 → json.dumps ValueError
    return [tc]


def _non_string_key_tool_calls() -> list[dict]:
    return [{frozenset({1, 2}): "v"}]  # 非字符串键 → json.dumps TypeError（default 不救键）


@pytest.mark.parametrize(
    "tool_calls",
    [_circular_tool_calls(), _non_string_key_tool_calls()],
    ids=["circular-ref", "non-string-key"],
)
async def test_unserializable_tool_calls_do_not_break_call(tool_calls):
    """消息诊断日志遇不可序列化 tool_calls → 回退 str()，调用照常完成。"""
    caller = _RecordingCaller(_llm_data(text="done"))
    plugin = _make_plugin(caller)

    result = await plugin.execute(
        _Ctx({"messages": [{"role": "assistant", "tool_calls": tool_calls}]})
    )

    assert result["raw_result"] == "done"
    assert caller.calls[0][1]["args"]["messages"][0]["tool_calls"] is tool_calls


# ── 能力信封 fail-closed：形状异常显式抛错 ──────────────────────────


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        ("not-a-dict", "信封形状异常"),
        ({"success": False, "error": "quota exceeded"}, r"工具执行失败.*quota exceeded"),
        ({"success": True, "data": "nope"}, "返回形状异常"),
    ],
    ids=["non-dict-envelope", "failure-envelope", "non-dict-data"],
)
async def test_malformed_envelopes_fail_closed(reply, match):
    """信封非 dict / success=False / data 非 dict → RuntimeError 携带原始语义。"""
    caller = _RecordingCaller(reply)
    plugin = _make_plugin(caller)

    with pytest.raises(RuntimeError, match=match):
        await plugin.execute(_Ctx({"messages": []}))


# ── 多模态引用解析失败：文件不存在 / 读取失败 ────────────────────────

_MIN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_resolve_image_ref_missing_file_reports_reason(tmp_path):
    """本地引用指向不存在的文件 → 空 data URL + 明确原因（绝对路径与相对路径两形态）。"""
    missing_abs = str(tmp_path / "nope.png")

    for url in (missing_abs, "relative/nope.png"):
        data_url, reason = LLMCore._resolve_image_ref(url)
        assert data_url == ""
        assert reason == "文件不存在"


@pytest.mark.parametrize("broken_attr", ["read_bytes", "stat"], ids=["read-fails", "stat-fails"])
def test_resolve_image_ref_oserror_reports_read_failure(tmp_path, monkeypatch, broken_attr):
    """存在但读取/取大小失败（OSError）→ "文件读取失败"，不静默跳过。"""
    png = tmp_path / "pic.png"
    png.write_bytes(_MIN_PNG)

    def _boom(*args, **kwargs):
        raise OSError("io failed")

    monkeypatch.setattr(Path, broken_attr, _boom)

    data_url, reason = LLMCore._resolve_image_ref(str(png))

    assert (data_url, reason) == ("", "文件读取失败")
