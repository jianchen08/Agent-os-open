# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage

"""notify_judge 行为测试——判定瀑布（查看态门/防双响/快切/LLM 主判/失败降级）。

断行为（输入→state_updates/LLM 通道载荷），不碰内部实现细节。LLM 经
set_capability_caller 注入 fake、查看态经 set_view_checker 注入 fake
（外部依赖 mock，合法边界）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))


def _load_plugin() -> Any:
    """按显式路径加载 plugin.py（裸名 plugin 全车道共跑会被劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "notify_judge_plugin_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "plugin.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod() -> Any:
    return _load_plugin()


# ═══════════════ 构造辅助 ═══════════════


def _assistant(text: str, seq: int | None = None, tool_calls: Any = None) -> dict:
    """tool_calls 形参收 Any：畸形形态（非列表/脏条目）是守卫分支的合法输入。"""
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    if seq is not None:
        msg["seq"] = seq
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_call(name: str, args: str = "{}") -> dict:
    return {"id": "tc_1", "type": "function", "function": {"name": name, "arguments": args}}


def _state(messages: list[dict]) -> dict[str, Any]:
    return {"messages": messages, "pipeline_id": "pipe-1"}


def _recording_caller(
    payload: str = '{"worthy": true, "reason": "交付"}',
    *,
    calls: list[dict] | None = None,
    envelope: dict | None = None,
    raise_exc: Exception | None = None,
    resolved_model: str = "tier-small-resolved",
):
    """fake capability caller：记录调用并按 tool_name 应答（外部依赖 mock，合法边界）。

    model.resolve → 返回 resolved_model（空串=解析为空）；其余（llm.complete_stream）
    按原互斥三态应答。
    """

    async def fake(method: str, params: dict, timeout: float | None = None) -> Any:
        if calls is not None:
            calls.append({"method": method, "params": params, "timeout": timeout})
        if params.get("tool_name") == "model.resolve":
            return {"success": True, "data": {"model_id": resolved_model}}
        if raise_exc is not None:
            raise raise_exc
        if envelope is not None:
            return envelope
        return {"success": True, "data": {"text": payload}}

    return fake


def _complete_stream_call(calls: list[dict]) -> dict:
    """取唯一一次 llm.complete_stream 调用（缺省模型路径会先经 model.resolve）。"""
    hits = [c for c in calls if c["params"].get("tool_name") == "llm.complete_stream"]
    assert len(hits) == 1, f"应恰一次 llm.complete_stream，实际 {len(hits)}"
    return hits[0]


def _sole_op(updates: dict[str, Any]) -> dict:
    """取 updates 里唯一一条 messages set-op。"""
    ops = updates["messages"]["_ops"]
    assert len(ops) == 1, f"应恰一条 set-op，实际 {ops}"
    assert ops[0]["op"] == "set"
    return ops[0]


def _run(judge: Any, state: dict[str, Any]) -> dict[str, Any]:
    """跑判定协程（同步测试内 asyncio.run，task_reminder 测试同款）。"""
    import asyncio

    return asyncio.run(judge.judge(state))


def _worthy_flag(op: dict) -> Any:
    meta = op["msg"].get("metadata")
    assert isinstance(meta, dict)
    return meta.get("notify_worthy")


# ═══════════════ 规则0：查看态门（用户正在看 → 零 LLM 零打标跳过） ═══════════════


def _view_checker_factory(
    viewed: bool,
    *,
    calls: list[dict] | None = None,
    raise_exc: Exception | None = None,
):
    """fake 查看态查询 caller（is_pipeline_viewed）：记录调用并应答 viewed 真值。"""

    async def fake(method: str, params: dict, timeout: float | None = None) -> Any:
        if calls is not None:
            calls.append({"method": method, "params": params, "timeout": timeout})
        if raise_exc is not None:
            raise raise_exc
        return {"pipeline_id": params.get("pipeline_id"), "viewed": viewed}

    return fake


def test_viewed_session_skips_judgment_without_llm(mod):
    """查看态 viewed=true → 零写入返回，且 LLM 通道一次都不被调。"""
    llm_calls: list[dict] = []
    view_calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=llm_calls))
    mod.set_view_checker(_view_checker_factory(True, calls=view_calls))
    messages = [_assistant("已经帮你设置好提醒了，到时候叫你", seq=7)]

    updates = _run(mod.NotifyJudge(), _state(messages))

    assert updates == {}, "查看态门必须零写入整步跳过"
    assert llm_calls == [], "查看态门必须零 LLM"
    assert len(view_calls) == 1
    assert view_calls[0]["params"] == {"pipeline_id": "pipe-1"}


def test_not_viewed_session_judges_normally(mod):
    """不在场 viewed=false → 回退既有判定链（快切命中照常打标）。"""
    mod.set_capability_caller(_recording_caller(calls=[]))
    mod.set_view_checker(_view_checker_factory(False))
    messages = [_assistant("已经帮你设置好提醒了，到时候叫你", seq=7)]

    updates = _run(mod.NotifyJudge(), _state(messages))

    assert _worthy_flag(_sole_op(updates)) is True


def test_view_check_failure_falls_back_to_judgment(mod):
    """查看态查询异常（旧内核无方法）→ 按不在场回退判定链，不减通知面。"""
    mod.set_capability_caller(_recording_caller(calls=[]))
    mod.set_view_checker(_view_checker_factory(True, raise_exc=RuntimeError("unknown method")))
    messages = [_assistant("已经帮你设置好提醒了，到时候叫你", seq=7)]

    updates = _run(mod.NotifyJudge(), _state(messages))

    assert _worthy_flag(_sole_op(updates)) is True


def test_state_without_pipeline_id_skips_view_gate(mod):
    """state 缺 pipeline_id → 查询无从发起，门不生效照常判定。"""
    llm_calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=llm_calls))
    mod.set_view_checker(_view_checker_factory(True, calls=[]))
    # 非快切词文本：判定必须走 LLM 主判（同时验证门跳过后主判链完整）。
    messages = [_assistant("分析完成，结论见正文", seq=7)]
    state = {"messages": messages}  # 无 pipeline_id

    updates = _run(mod.NotifyJudge(), state)

    assert _worthy_flag(_sole_op(updates)) is True
    assert llm_calls, "无 pipeline_id 不查查看态，直接进判定链（LLM 主判）"


# ═══════════════ 规则1：human_interaction 防双响 ═══════════════


@pytest.mark.parametrize(
    "final_text",
    [
        "已经帮你设置好提醒了，到时候叫你",  # 即便同时命中快切词，防双响仍优先
        "有一个确认卡片等你处理",
    ],
)
def test_human_interaction_in_tail_suppresses_notify(mod, final_text):
    """规则1：尾部窗口有 human_interaction 调用 → 零写入（卡片路径已通知）。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    messages = [
        {"role": "user", "content": "帮我设个提醒"},
        _assistant("好的", seq=11, tool_calls=[_tool_call("human_interaction")]),
        _assistant(final_text, seq=12),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert updates == {}, "防双响必须零写入"
    assert calls == [], "防双响短路后不得调 LLM"


# ═══════════════ 规则2：快切词表（跳过 LLM） ═══════════════


@pytest.mark.parametrize("keyword", ["已设置提醒", "到时候叫你", "别忘了"])
def test_fast_cut_keyword_writes_notify_worthy(mod, keyword):
    """规则2：最终文本命中词表 → 直判弹，不调 LLM。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    messages = [
        {"role": "user", "content": "记得提醒我开会"},
        _assistant(f"放心，{keyword}，到点我会处理。", seq=7),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    op = _sole_op(updates)
    assert op["seq"] == 7
    assert _worthy_flag(op) is True
    assert calls == [], "快切命中不得调 LLM"


def test_fast_cut_casefold_latin_keyword(mod):
    """快切对拉丁词表大小写折叠（词表可配扩展面）。"""
    mod.set_capability_caller(_recording_caller())
    judge = mod.NotifyJudge({"settings": {"keywords": ["Done✅"]}})
    messages = [{"role": "user", "content": "hi"}, _assistant("all done✅", seq=3)]
    updates = _run(judge, _state(messages))
    assert _worthy_flag(_sole_op(updates)) is True


# ═══════════════ 规则3：LLM 主判 ═══════════════


def test_llm_verdict_true_writes_notify_worthy(mod):
    """规则3：无快切命中 → LLM 判是 → 写 metadata。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller('```json\n{"worthy": true, "reason": "向用户提问"}\n```', calls=calls))
    messages = [
        {"role": "user", "content": "看看构建状态"},
        _assistant("查一下", seq=10, tool_calls=[_tool_call("bash", '{"cmd": "ls"}')]),
        _assistant("构建失败了，需要你确认是否回滚？", seq=11),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    op = _sole_op(updates)
    assert op["seq"] == 11
    assert op["msg"]["content"] == "构建失败了，需要你确认是否回滚？"
    assert _worthy_flag(op) is True

    # LLM 通道契约：缺省模型先 model.resolve(small) 单源解析，再 llm.complete_stream。
    assert len(calls) == 2
    assert calls[0]["method"] == "invoke"
    assert calls[0]["params"]["tool_name"] == "model.resolve"
    assert calls[0]["params"]["plugin_id"] == "llm_service"
    assert calls[0]["params"]["args"] == {"model_tier": "small"}
    params = _complete_stream_call(calls)["params"]
    assert params["plugin_id"] == "llm_service"
    assert params["args"]["model"] == "tier-small-resolved"
    # 判定调用不携带 _call_context（流式事件路由键空 → 内核丢弃，不污染前端）。
    assert "_call_context" not in params
    prompt = params["args"]["messages"][0]["content"]
    assert "值得打断用户" in prompt
    assert "[用户] 看看构建状态" in prompt
    assert "[工具调用] bash(" in prompt
    assert "[助手] 构建失败了" in prompt


def test_llm_verdict_false_writes_nothing(mod):
    """规则3：LLM 判否 → 零写入（true-only 契约：false 不落 metadata）。"""
    mod.set_capability_caller(_recording_caller('{"worthy": false, "reason": "中间进度"}'))
    messages = [
        {"role": "user", "content": "继续"},
        _assistant("收到，我开始处理下一步。", seq=5),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert updates == {}


def test_llm_prompt_tail_truncation_keeps_latest(mod):
    """转录超上限保尾截断：最新输出保留，最早内容丢弃。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    filler = "过程碎语" * 200
    messages = [
        {"role": "user", "content": filler},
        _assistant("结尾结论：" + "x" * 50, seq=2),
    ]
    judge = mod.NotifyJudge({"settings": {"max_prompt_chars": 300}})
    _run(judge, _state(messages))
    prompt = _complete_stream_call(calls)["params"]["args"]["messages"][0]["content"]
    assert "结尾结论" in prompt, "保尾截断必须保留最新输出"
    assert len(prompt) < len(filler), "截断必须生效"


def test_llm_verdict_with_custom_model_from_settings(mod):
    """模型档从 settings 读取（钉死具体模型时直用、不多走 model.resolve）。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    judge = mod.NotifyJudge({"settings": {"model": "deepseek-v4-flash"}})
    messages = [{"role": "user", "content": "hi"}, _assistant("正在继续。", seq=1)]
    _run(judge, _state(messages))
    assert calls[0]["params"]["tool_name"] == "llm.complete_stream"
    assert calls[0]["params"]["args"]["model"] == "deepseek-v4-flash"


def test_default_model_resolves_small_tier_via_model_resolve(mod):
    """缺省不钉模型：先 model.resolve(small) 单源解析，再用解析结果调判定。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    messages = [{"role": "user", "content": "hi"}, _assistant("正在继续。", seq=1)]
    _run(mod.NotifyJudge(), _state(messages))
    assert [c["params"]["tool_name"] for c in calls] == ["model.resolve", "llm.complete_stream"]


def test_small_tier_unresolvable_degrades_silent(mod):
    """small 档解析为空（llm.yaml 无 defaults）→ 判定失败静默，禁自行 fallback 具体模型。"""
    mod.set_capability_caller(_recording_caller(resolved_model=""))
    messages = [
        {"role": "user", "content": "继续"},
        _assistant("这一步做完了，接着下一步。", seq=9),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert updates == {}


def test_max_tokens_default_2000(mod):
    """max_tokens 缺省 2000：推理模型 thinking 吃预算，200 写死致 content 空。

    装机实测（minimax-m3 推理档）：200 token 全被 thinking 消耗 → 正文空 →
    「LLM 判定输出非 JSON:(空)」全天 14 次降级 worthy=true。
    """
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    messages = [{"role": "user", "content": "hi"}, _assistant("正在继续。", seq=1)]
    _run(mod.NotifyJudge(), _state(messages))
    assert _complete_stream_call(calls)["params"]["args"]["max_tokens"] == 2000


def test_max_tokens_configurable_from_settings(mod):
    """max_tokens 从 settings 读取（不同模型档 thinking 预算不同）。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    judge = mod.NotifyJudge({"settings": {"max_tokens": 800}})
    messages = [{"role": "user", "content": "hi"}, _assistant("正在继续。", seq=1)]
    _run(judge, _state(messages))
    assert _complete_stream_call(calls)["params"]["args"]["max_tokens"] == 800


# ═══════════════ 规则4：LLM 失败降级静默不弹（fail-closed 减噪） ═══════════════


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"raise_exc": TimeoutError("llm timeout")}, id="调用抛异常"),
        pytest.param(
            {"envelope": {"success": False, "error": "no healthy deployments"}},
            id="信封失败",
        ),
        pytest.param({"payload": "我觉得还行"}, id="输出非 JSON"),
        pytest.param(
            {"payload": '{"reason": "缺 worthy"}'},
            id="缺 worthy 布尔字段",
        ),
    ],
)
def test_llm_failure_degrades_to_silent(mod, kwargs):
    """规则4：LLM 失败（异常/信封失败/输出畸形）→ 零打标（不弹）。

    v4 语义翻转：判定器职责是减噪，失败即全量放行等于减噪器失效——装机实测
    2026-10-20 单日 17 次超时/空输出降级，每次降级=一次无谓打扰。
    """
    mod.set_capability_caller(_recording_caller(**kwargs))
    messages = [
        {"role": "user", "content": "继续"},
        _assistant("这一步做完了，接着下一步。", seq=9),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert updates == {}, "判定失败必须静默（零打标）"


def test_llm_failure_still_honours_fast_cut(mod):
    """判定失败不弹，但快切词表在前置覆盖——明确承诺/提醒场景仍弹。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(raise_exc=TimeoutError("llm timeout"), calls=calls))
    messages = [
        {"role": "user", "content": "帮我设个提醒"},
        _assistant("已设置提醒，到点会通知你。", seq=9),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    op = _sole_op(updates)
    assert op["seq"] == 9
    assert _worthy_flag(op) is True
    assert calls == [], "快切命中不得调 LLM"


# ═══════════════ 规则0：终局门（带工具调用的中间轮不判定） ═══════════════


def test_tool_call_round_zero_write_and_no_llm(mod):
    """规则0：尾条 assistant 带 tool_calls → 零判定零打标，且不调 LLM。

    带调用的 assistant 消息=中间轮（本轮还要继续调工具，尚未交付）；对它
    弹通知即「每个 AI 回复都弹」。装机库实测 120 条打标里 43 条带 tool_calls。
    """
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    messages = [
        {"role": "user", "content": "看看构建状态"},
        _assistant("我先查一下", seq=10, tool_calls=[_tool_call("bash", '{"cmd": "ls"}')]),
    ]
    assert _run(mod.NotifyJudge(), _state(messages)) == {}
    assert calls == [], "中间轮不得调 LLM"


def test_tool_call_round_with_keyword_zero_write(mod):
    """规则0 优先于快切：中间轮即便命中词表也不打标（非终局交付）。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    messages = [
        {"role": "user", "content": "帮我设个提醒"},
        _assistant(
            "已设置提醒，马上就去办",
            seq=10,
            tool_calls=[_tool_call("bash", '{"cmd": "date"}')],
        ),
    ]
    assert _run(mod.NotifyJudge(), _state(messages)) == {}
    assert calls == []


def test_terminal_round_after_tool_call_still_judged(mod):
    """规则0 只看尾条：调用轮之后的纯文本终局轮照常判定。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller('{"worthy": true, "reason": "交付"}', calls=calls))
    messages = [
        {"role": "user", "content": "看看构建状态"},
        _assistant("我先查一下", seq=10, tool_calls=[_tool_call("bash", '{"cmd": "ls"}')]),
        _assistant("构建失败了，需要你确认是否回滚？", seq=11),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    op = _sole_op(updates)
    assert op["seq"] == 11
    assert _worthy_flag(op) is True
    assert len(calls) == 2


# ═══════════════ 产出面：metadata op 形态 ═══════════════


def test_metadata_merge_preserves_existing_keys(mod):
    """worthy 写入合并既有 metadata，不整体替换。"""
    mod.set_capability_caller(_recording_caller())
    messages = [
        {"role": "user", "content": "hi"},
        _assistant("交付完成✅", seq=4),
    ]
    messages[-1]["metadata"] = {"agent_id": "main", "notify_worthy": False}
    updates = _run(mod.NotifyJudge(), _state(messages))
    meta = _sole_op(updates)["msg"]["metadata"]
    assert meta["notify_worthy"] is True
    assert meta["agent_id"] == "main"


def test_final_assistant_without_seq_zero_write(mod):
    """尾条 assistant 无 seq（不可定位槽位）→ 零写入，不产畸形 op。"""
    mod.set_capability_caller(_recording_caller())
    messages = [
        {"role": "user", "content": "hi"},
        _assistant("交付完成✅"),  # 无 seq
    ]
    assert _run(mod.NotifyJudge(), _state(messages)) == {}


# ═══════════════ 守卫面：enabled / 无正文 / 配置健壮性 ═══════════════


def test_disabled_zero_write_and_no_llm(mod):
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    judge = mod.NotifyJudge({"settings": {"enabled": False}})
    messages = [{"role": "user", "content": "hi"}, _assistant("交付完成✅", seq=1)]
    assert _run(judge, _state(messages)) == {}
    assert calls == []


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param(
            [
                {"role": "user", "content": "hi"},
                _assistant("", seq=1, tool_calls=[_tool_call("bash")]),
            ],
            id="纯工具调用轮无正文",
        ),
        pytest.param([{"role": "user", "content": "只有用户消息"}], id="无 assistant 产出"),
        pytest.param([], id="空消息表"),
    ],
)
def test_no_judgeable_output_zero_write(mod, messages):
    """无可判定的 assistant 正文 → 零写入且不调 LLM。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    assert _run(mod.NotifyJudge(), _state(messages)) == {}
    assert calls == []


def test_multimodal_content_parts_joined(mod):
    """multimodal list 内容取 text 部件参与快切。"""
    mod.set_capability_caller(_recording_caller())
    final = {
        "role": "assistant",
        "seq": 6,
        "content": [{"type": "text", "text": "已设置提醒"}, {"type": "image_url"}],
    }
    updates = _run(mod.NotifyJudge(), _state([{"role": "user", "content": "hi"}, final]))
    assert _worthy_flag(_sole_op(updates)) is True


def test_tail_window_configurable_rounds(mod):
    """tail_rounds 决定窗口起点：窗口外的旧 human_interaction 不防双响。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller('{"worthy": false, "reason": "普通续作"}', calls=calls))
    # 尾部 2 轮：a2(a3 之前) 与 a3；human_interaction 在 a1（窗口外）。
    messages = [
        {"role": "user", "content": "起手"},
        _assistant("先问一下", seq=1, tool_calls=[_tool_call("human_interaction")]),
        {"role": "user", "content": "继续"},
        _assistant("好的继续", seq=2),
        _assistant("这一步完成了。", seq=3),
    ]
    updates = _run(mod.NotifyJudge({"settings": {"tail_rounds": 2}}), _state(messages))
    assert updates == {}  # LLM 判否 → 零写入（human_interaction 未拦）
    prompt = _complete_stream_call(calls)["params"]["args"]["messages"][0]["content"]
    assert "[用户] 继续" in prompt, "窗口应含当前输入 user 消息"
    assert "先问一下" not in prompt, "窗口外的旧轮不得进入转录"


def test_silent_tools_configurable(mod):
    """防双响工具面可配（不止 human_interaction）。"""
    mod.set_capability_caller(_recording_caller())
    judge = mod.NotifyJudge({"settings": {"silent_tools": ["request_approval"]}})
    messages = [
        {"role": "user", "content": "hi"},
        _assistant("先提审批", seq=1, tool_calls=[_tool_call("request_approval")]),
        _assistant("已提交审批✅", seq=2),
    ]
    assert _run(judge, _state(messages)) == {}


def test_json_verdict_extraction_tolerates_surroundings(mod):
    """LLM 输出带前后缀时仍能取到 JSON 对象（宽容提取）。"""
    verdict = mod.NotifyJudge._parse_verdict('前缀说明 {"worthy": false, "reason": "r"} 后缀')
    assert verdict is False


def test_missing_settings_namespace_falls_back_to_defaults(mod):
    """settings 命名空间缺失（配置文件缺载）→ 代码默认值可用，不炸。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    judge = mod.NotifyJudge({"unrelated": {}})
    messages = [{"role": "user", "content": "hi"}, _assistant("交付完成✅", seq=1)]
    updates = _run(judge, _state(messages))
    assert _worthy_flag(_sole_op(updates)) is True
    assert calls == []  # 缺省词表快切命中，未走到 LLM


def test_op_shape_matches_engine_contract(mod):
    """op 形态 = 引擎 messages._ops 协议（set + seq + 整条 msg）。"""
    mod.set_capability_caller(_recording_caller())
    final = _assistant("到时候叫你", seq=8)
    final["reasoning_content"] = "思考"
    updates = _run(mod.NotifyJudge(), _state([{"role": "user", "content": "定个闹钟"}, final]))
    op = _sole_op(updates)
    assert set(op.keys()) == {"op", "seq", "msg"}
    assert op["msg"]["role"] == "assistant"
    assert op["msg"]["reasoning_content"] == "思考"
    # msg 全文可 JSON 序列化（blob 落库前提）。
    json.dumps(op["msg"], ensure_ascii=False)


# ═══════════════ 守卫分支与窗口边界 ═══════════════


def test_messages_non_list_zero_write(mod):
    """state.messages 形态异常（非列表）→ 零写入直通。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    assert _run(mod.NotifyJudge(), {"messages": "junk"}) == {}
    assert calls == []


def test_window_clamps_when_rounds_exceed_assistant_count(mod):
    """tail_rounds 超出实际 assistant 数 → 窗口起点钳到 0，不越界。"""
    judge = mod.NotifyJudge({"settings": {"tail_rounds": 5}})
    messages = [
        {"role": "user", "content": "u0"},
        _assistant("a1", seq=1),
        {"role": "user", "content": "u1"},
        _assistant("a2", seq=2),
    ]
    window = judge._tail_window(messages)
    assert window[0]["content"] == "u0"
    assert len(window) == 4


def test_window_walkback_crosses_previous_assistant_to_user(mod):
    """连续 assistant 轮（恢复续跑形态）回溯越过前轮到 user 消息。"""
    judge = mod.NotifyJudge({"settings": {"tail_rounds": 1}})
    messages = [
        {"role": "user", "content": "u0"},
        _assistant("a1", seq=1),
        _assistant("a2", seq=2),
    ]
    window = judge._tail_window(messages)
    assert [m["content"] for m in window] == ["u0", "a1", "a2"]


def test_window_filters_non_dict_entries(mod):
    """messages 混入非 dict 元素 → 窗口过滤，不炸。"""
    judge = mod.NotifyJudge()
    window = judge._tail_window(["junk", None, _assistant("a1", seq=1)])
    assert window == [{"role": "assistant", "content": "a1", "seq": 1}]


def test_window_empty_messages_returns_empty(mod):
    """空消息表 → 空窗口（边界输入）。"""
    assert mod.NotifyJudge()._tail_window([]) == []


def test_no_caller_degrades_to_silent(mod):
    """LLM 通道未接线（caller 缺失）→ 降级静默不弹。"""
    mod.set_capability_caller(None)
    messages = [{"role": "user", "content": "hi"}, _assistant("正在继续。", seq=1)]
    assert _run(mod.NotifyJudge(), _state(messages)) == {}


def test_envelope_data_non_dict_degrades_to_silent(mod):
    """信封 data 形态异常 → 降级静默不弹。"""
    mod.set_capability_caller(_recording_caller(envelope={"success": True, "data": "oops"}))
    messages = [{"role": "user", "content": "hi"}, _assistant("正在继续。", seq=1)]
    assert _run(mod.NotifyJudge(), _state(messages)) == {}


def test_message_text_none_content_is_empty(mod):
    """content=None（无正文部件）→ 空文本，无可判定面零写入。"""
    mod.set_capability_caller(_recording_caller())
    final = {"role": "assistant", "content": None, "seq": 1}
    assert _run(mod.NotifyJudge(), _state([{"role": "user", "content": "hi"}, final])) == {}


def test_tool_call_summaries_malformed_and_truncation(mod):
    """工具调用摘要：畸形条目跳过、dict 参数序列化、超长参数截断。

    摘要取自窗口内更早的 assistant 轮（尾条必须是终局交付轮才进判定）。
    """
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller('{"worthy": false, "reason": "过程"}', calls=calls))
    junk_calls = [
        None,
        {"function": "junk"},
        {"function": {"name": "bash", "arguments": {"cmd": "x" * 500}}},
    ]
    messages = [
        {"role": "user", "content": "hi"},
        _assistant("看看", seq=1, tool_calls="not-a-list"),
        _assistant("继续", seq=2, tool_calls=junk_calls),
        _assistant("这一步做完了。", seq=3),
    ]
    _run(mod.NotifyJudge(), _state(messages))
    prompt = _complete_stream_call(calls)["params"]["args"]["messages"][0]["content"]
    assert prompt.count("[工具调用]") == 1, "非列表 tool_calls 不产摘要，畸形条目跳过"
    line = next(ln for ln in prompt.splitlines() if ln.startswith("[工具调用] bash("))
    args_part = line[len("[工具调用] bash(") : -1]
    assert args_part.startswith('{"cmd": "')
    assert len(args_part) <= 121, "参数预览按上限截断"


# ═══════════════ 触发来源轮次：零打标零判定 ═══════════════


def test_trigger_origin_zero_write_and_no_llm(mod):
    """触发器注入轮次：收尾文本不自动打标（是否通知由 agent 经交互卡决定）。"""
    calls: list[dict] = []
    mod.set_capability_caller(_recording_caller(calls=calls))
    state = _state([_assistant("到点了", seq=1)])
    state["trigger.origin"] = "trigger"
    state["trigger.source"] = "mode_personal"
    assert _run(mod.NotifyJudge(), state) == {}
    assert calls == []


# ════════════════════════════════════════════════════════════
# 通知出队冲刷（批次 5：管道空闲即冲刷，流式中押单不变）
# ════════════════════════════════════════════════════════════


def _flush_fakes(
    active: bool | None = False,
    raise_exc: Exception | None = None,
):
    """fake 活跃查询 + 事件发射：记录 notify_flush 发射与活跃查询。"""
    flushed: list[dict] = []
    activity_calls: list[dict] = []

    async def activity(method: str, params: dict, timeout: float | None = None) -> Any:
        activity_calls.append({"method": method, "params": params})
        if raise_exc is not None:
            raise raise_exc
        return {"pipeline_id": params.get("pipeline_id"), "active": active}

    async def emitter(event: str, payload: dict, timeout: float | None = None) -> Any:
        flushed.append({"event": event, "payload": payload})

    return activity, emitter, flushed, activity_calls


def _wire_flush(mod: Any, active: bool | None = False, raise_exc: Exception | None = None):
    """接线冲刷 fake 并清记账（模块级单例，逐用例复位）。"""
    activity, emitter, flushed, activity_calls = _flush_fakes(active, raise_exc)
    mod.set_activity_checker(activity)
    mod.set_event_emitter(emitter)
    mod.reset_flush_bookkeeping_for_test()
    return flushed, activity_calls


def test_idle_pipeline_flushes_notify_immediately(mod: Any) -> None:
    """空闲（无在途链）→ worthy 打标同时补发 notify_flush（立即出队，不等下一事件）。"""
    flushed, activity_calls = _wire_flush(mod, active=False)
    mod.set_capability_caller(_recording_caller(calls=[]))
    mod.set_view_checker(_view_checker_factory(False))
    messages = [
        {"role": "user", "content": "把报告整理好"},
        _assistant("报告搞定了，已经发你邮箱。", seq=7),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert _worthy_flag(_sole_op(updates)) is True, "打标照常落库"
    assert len(flushed) == 1, f"空闲应立即冲刷一次，实际 {len(flushed)}"
    assert flushed[0]["event"] == "notify_flush"
    payload = flushed[0]["payload"]
    assert payload["notify_worthy"] is True
    assert payload["pipeline_id"] == "pipe-1"
    assert activity_calls and activity_calls[0]["params"]["pipeline_id"] == "pipe-1"


def test_streaming_pipeline_holds_flush(mod: Any) -> None:
    """流式进行中（活跃链在场）→ 押单不打断流：不发射，打标照常。"""
    flushed, _ = _wire_flush(mod, active=True)
    mod.set_capability_caller(_recording_caller(calls=[]))
    mod.set_view_checker(_view_checker_factory(False))
    messages = [
        {"role": "user", "content": "继续"},
        _assistant("搞定了第一段。", seq=3),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert _worthy_flag(_sole_op(updates)) is True, "押单仍打标（帧链路/后续捎出兜底）"
    assert flushed == [], "流式进行中不得补发（押单语义不变）"


def test_long_idle_flushes_once_not_repeated(mod: Any) -> None:
    """长空闲只冲一次：同消息重复判定（重跑/重放）命中记账不双发。"""
    flushed, _ = _wire_flush(mod, active=False)
    mod.set_capability_caller(_recording_caller(calls=[]))
    mod.set_view_checker(_view_checker_factory(False))
    messages = [
        {"role": "user", "content": "提醒事项"},
        _assistant("放心，已设置提醒，到时候叫你。", seq=5),
    ]
    state = _state(messages)
    _run(mod.NotifyJudge(), state)
    _run(mod.NotifyJudge(), state)
    _run(mod.NotifyJudge(), state)
    assert len(flushed) == 1, f"同消息三轮判定应只冲一次，实际 {len(flushed)}"


def test_not_worthy_never_flushes(mod: Any) -> None:
    """LLM 判否（不值得打断）→ 零打标零发射（出队只随 worthy 面）。"""
    flushed, _ = _wire_flush(mod, active=False)
    mod.set_capability_caller(_recording_caller(payload='{"worthy": false, "reason": "碎语"}'))
    mod.set_view_checker(_view_checker_factory(False))
    messages = [
        {"role": "user", "content": "继续"},
        _assistant("正在继续处理中。", seq=2),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert updates == {}, "判否零写入"
    assert flushed == [], "非 worthy 不发射"


def test_flush_activity_query_failure_treated_as_idle(mod: Any) -> None:
    """活跃查询失败 → 按空闲冲刷（通知面只增不减，与查看态门同口径）。"""
    flushed, _ = _wire_flush(mod, active=False, raise_exc=RuntimeError("unknown method"))
    mod.set_capability_caller(_recording_caller(calls=[]))
    mod.set_view_checker(_view_checker_factory(False))
    messages = [
        {"role": "user", "content": "交付"},
        _assistant("全部搞定了✅。", seq=4),
    ]
    updates = _run(mod.NotifyJudge(), _state(messages))
    assert _worthy_flag(_sole_op(updates)) is True
    assert len(flushed) == 1, "查询失败按空闲立即冲刷（不减通知面）"
