# @feature: FP-T07 llm api | @ci: python-coverage
"""MiniMax 原生工具调用正文兜底解析（BUG-17）行为测试。

契约：MiniMax-M2/M3 系把工具调用以特殊标记（``<minimax:tool_call>`` 容器 +
invoke/parameter 子标签）承载。当请求未携带 tools 声明或上游未激活 tool-call
解析时，调用意图以正文到达——标记还可能呈转义倾倒形态
``]<]minimax[>[<invoke ...>``（生产 2026-09-15 实测字节）。adapter 必须在
结构化 tool_calls 缺席时把正文中的调用块解析回结构化 tool_calls 并从正文
剥离，否则调用意图泄漏为 UI 正文且工具永不执行。

覆盖行为面：
- 转义倾倒形态（生产实测字节，含漏写 parameter 闭合的模型输出）
- 容器内裸 JSON 第四形态（16:52 生产字节；arguments 对象/JSON 字符串两态、
  值内嵌套 JSON 容错、非 JSON 体不识别不剥离）
- 规范文档形态（<minimax:tool_call> + 纯净 <invoke>/<parameter>）
- 多 invoke 块按序解析；arguments 为 JSON 字符串且 json.loads 还原参数表
- 结构化 tool_calls 已存在 → 不解析不重复、正文原样保留
- 非 minimax 模型 → 不解析（门禁）；无标记文本 → 零副作用
- adapter 集成：流式聚合（_call_streaming 桩流）与非流式路径均触发恢复

外部依赖全桩（litellm 流经 _FakeStream 桩），解析逻辑全真实实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
# 本插件目录置 sys.path[0]：车道共跑时其他插件同名 adapter.py 目录可能占住前位
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)

# 只逐出 adapter（本文件唯一裸名导入）：exceptions/key_pool/router_factory
# 不得逐出——字母序在本文件之前收集的用例（test_key_unresolved_failclosed）
# 对这些模块对象做属性 patch，逐出会使其运行期惰性 import 解析到新实例、
# 身份分裂（全车道共跑实证 3 红）。
sys.modules.pop("adapter", None)

import adapter as adapter_mod  # noqa: E402  平铺 import，与生产代码一致


class _NoHandlersLogger(logging.Logger):
    """``.handlers`` 恒为空：屏蔽 pytest 日志插件挂的 handler（同 tests/ 根惯例）。"""

    @property
    def handlers(self) -> list[Any]:
        return []

    @handlers.setter
    def handlers(self, value: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_diag_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter_mod, "_diag_logger", _NoHandlersLogger("adapter._diag.mmrecovery")
    )
    monkeypatch.setattr(
        adapter_mod, "_stream_logger", _NoHandlersLogger("adapter._stream.mmrecovery")
    )


# ─────────────────────────── 生产实测文本（kernel db 字节原样） ───────────────────────────

# 转义倾倒形态：open 标记 <minimax: → ]<]minimax[>[<；goal_title 参数漏写
# parameter 闭合（模型自己的输出瑕疵），</invoke> 以纯净形态到达。
_MANGLED_PRODUCTION_TEXT = (
    "\n\n好嘞～这个任务很明确，我直接派给 general_agent 帮你执行一下～"
    "]<]minimax[>[<tool_call>\n"
    "]<]minimax[>[<invoke name=\"task_submit\">\n"
    "]<]minimax[>[<parameter name=\"goal\">执行 bash 命令 `echo R37T02_DONE`，"
    "把命令的标准输出原样作为本次执行结果返回给上级。]"
    "<]minimax[>[</parameter>\n"
    "]<]minimax[>[<parameter name=\"agent_type\">general_agent</parameter>\n"
    "]<]minimax[>[<parameter name=\"goal_title\">执行echo命令并返回输出</goal_title>\n"
    "</invoke>\n"
    "]<]minimax[>[</tool_call>"
)

# 规范文档形态（MiniMax 官方 tool calling guide）
_CANONICAL_TEXT = (
    "我来查一下。\n"
    "<minimax:tool_call>\n"
    '<invoke name="get_weather">\n'
    '<parameter name="city">北京</parameter>\n'
    '<parameter name="unit">celsius</parameter>\n'
    "</invoke>\n"
    "</minimax:tool_call>"
)

# 第四形态：容器内裸 JSON（生产 2026-09-15 16:52 管道 71733df268af 实测字节，
# kernel db blob 5d0757593e0f 原样）——open 标记转义倾倒、JSON 体、纯净 </tool_call>。
_BARE_JSON_PRODUCTION_TEXT = (
    "\n\n好哒～这就帮你派发一个子任务去执行这条命令，让它把输出原样带回来～\n\n"
    "让我先把这件小事安排给 general_agent 来办：]<]minimax[>[<tool_call>\n"
    "{\"name\": \"task_submit\", \"arguments\": {\"goal\": \"执行 bash 命令 `echo R37T02_DONE`，"
    "把命令的标准输出原样返回。背景：用户要求验证子任务执行 bash 命令的能力并带回输出。\", "
    "\"agent_id\": \"general_agent\", \"goal_title\": \"执行bash命令并带回输出\"}}\n"
    "</tool_call>"
)


def _parse(text: str) -> tuple[list[dict[str, Any]], str]:
    return adapter_mod.parse_minimax_native_tool_calls(text)


# ─────────────────────────── 解析行为 ───────────────────────────


class TestParseNativeToolCalls:
    def test_mangled_production_bytes(self) -> None:
        """转义倾倒形态（生产实测字节）→ 1 个 task_submit 调用 + 正文剥离。"""
        tool_calls, cleaned = _parse(_MANGLED_PRODUCTION_TEXT)
        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc["name"] == "task_submit"
        assert tc["id"].startswith("call_")
        args = json.loads(tc["arguments"])
        assert args["agent_type"] == "general_agent"
        assert "echo R37T02_DONE" in args["goal"]
        # 漏写 parameter 闭合的 goal_title：容忍截取到下一边界
        assert args["goal_title"].startswith("执行echo命令并返回输出")
        # 正文剥离：无标记残留，前缀人话保留
        assert "minimax[>" not in cleaned
        assert "<minimax:" not in cleaned
        assert "好嘞～这个任务很明确" in cleaned

    def test_canonical_documented_form(self) -> None:
        """规范文档形态 → 参数完整解析（防拟合：与倾倒形态输入有区分度）。"""
        tool_calls, cleaned = _parse(_CANONICAL_TEXT)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "get_weather"
        args = json.loads(tool_calls[0]["arguments"])
        assert args == {"city": "北京", "unit": "celsius"}
        assert "我来查一下。" in cleaned
        assert "<minimax:tool_call>" not in cleaned

    def test_multiple_invokes_in_order(self) -> None:
        """单容器多 invoke → 按序解析为多个调用。"""
        text = (
            "]<]minimax[>[<tool_call>\n"
            "]<]minimax[>[<invoke name=\"alpha\">\n"
            "]<]minimax[>[<parameter name=\"x\">1</parameter>\n"
            "</invoke>\n"
            "]<]minimax[>[<invoke name=\"beta\">\n"
            "]<]minimax[>[<parameter name=\"y\">2</parameter>\n"
            "</invoke>\n"
            "]<]minimax[>[</tool_call>"
        )
        tool_calls, cleaned = _parse(text)
        assert [tc["name"] for tc in tool_calls] == ["alpha", "beta"]
        assert json.loads(tool_calls[0]["arguments"]) == {"x": "1"}
        assert json.loads(tool_calls[1]["arguments"]) == {"y": "2"}
        assert cleaned == ""

    def test_arguments_json_roundtrip(self) -> None:
        """性质断言：arguments 是 JSON 字符串且还原后等于参数表（键值序保持）。"""
        text = (
            "<minimax:tool_call>"
            '<invoke name="t">'
            '<parameter name="kebab-key">v-1</parameter>'
            '<parameter name="n">2</parameter>'
            "</invoke>"
            "</minimax:tool_call>"
        )
        tool_calls, _cleaned = _parse(text)
        args = json.loads(tool_calls[0]["arguments"])
        assert args == {"kebab-key": "v-1", "n": "2"}
        assert list(args.keys()) == ["kebab-key", "n"]

    def test_plain_text_untouched(self) -> None:
        """无标记文本 → 零 tool_calls、正文原样（零副作用）。"""
        text = "你好，今天天气不错。"
        tool_calls, cleaned = _parse(text)
        assert tool_calls == []
        assert cleaned == text

    def test_id_format_contract(self) -> None:
        """id 符合系统 call_<hex> 契约（llm_core 仅重写非标准 id）。"""
        import re

        tool_calls, _cleaned = _parse(_CANONICAL_TEXT)
        assert re.fullmatch(r"call_[0-9a-f]+", tool_calls[0]["id"])

    def test_bare_json_container_production_bytes(self) -> None:
        """第四形态：容器内裸 JSON（16:52 生产字节）→ 解析为结构化调用 + 正文剥离。"""
        tool_calls, cleaned = _parse(_BARE_JSON_PRODUCTION_TEXT)
        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc["name"] == "task_submit"
        assert tc["id"].startswith("call_")
        args = json.loads(tc["arguments"])
        assert args["agent_id"] == "general_agent"
        assert "echo R37T02_DONE" in args["goal"]
        assert args["goal_title"] == "执行bash命令并带回输出"
        # 正文剥离：无标记残留，人话前缀保留
        assert "minimax[>" not in cleaned
        assert "<tool_call>" not in cleaned
        assert "好哒～这就帮你派发" in cleaned
        assert "让我先把这件小事安排给 general_agent 来办：" in cleaned

    def test_bare_json_arguments_as_json_string(self) -> None:
        """arguments 为 JSON 字符串（而非对象）→ 解码后还原参数表。"""
        text = (
            '<minimax:tool_call>{"name": "t", '
            '"arguments": "{\\"x\\": 1, \\"y\\": \\"两\\"}"}</minimax:tool_call>'
        )
        tool_calls, _cleaned = _parse(text)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "t"
        assert json.loads(tool_calls[0]["arguments"]) == {"x": 1, "y": "两"}

    def test_bare_json_nested_json_value_survives(self) -> None:
        """容错：arguments 值里嵌套 JSON（转义内嵌）→ 值按字符串原样保留。"""
        inner = json.dumps({"cmd": "echo hi"}, ensure_ascii=False)
        text = (
            ']<]minimax[>[<tool_call>{"name": "run", "arguments": {"goal": '
            + json.dumps(inner, ensure_ascii=False)
            + "}}</tool_call>"
        )
        tool_calls, _cleaned = _parse(text)
        assert json.loads(tool_calls[0]["arguments"])["goal"] == inner

    def test_bare_json_unparseable_body_noop(self) -> None:
        """容器内既无 invoke 也非 JSON → 不识别不剥离（响应原样，不误吞正文）。"""
        text = "]<]minimax[>[<tool_call>\n这不是JSON也不是invoke\n</tool_call>"
        tool_calls, cleaned = _parse(text)
        assert tool_calls == []
        assert cleaned == text

    def test_bare_json_non_object_or_nameless_noop(self) -> None:
        """JSON 体但非对象（数组）/无 name/name 空串 → 不识别（防误执行）。"""
        for body in ('[1, 2]', '{"arguments": {}}', '{"name": ""}', '"just a string"'):
            text = f"]<]minimax[>[<tool_call>{body}</tool_call>"
            tool_calls, cleaned = _parse(text)
            assert tool_calls == [], body
            assert cleaned == text, body

    def test_completion_tools_outbound_log(self, caplog: Any) -> None:
        """工具面出站观测：tools 非空时 INFO 记录数量与名单（BUG-17 主路径断言点）。"""
        tools = [
            {"type": "function", "function": {"name": "alpha", "parameters": {}}},
            {"type": "function", "function": {"name": "beta", "parameters": {}}},
        ]
        chunks = [_chunk([_choice(_delta(content="ok"), "stop")])]
        ad = _StubAdapter(_FakeStream(chunks))
        with caplog.at_level(logging.INFO, logger="adapter"):
            _run(
                ad.completion(
                    "minimax/MiniMax-M3",
                    [{"role": "user", "content": "hi"}],
                    tools=tools,
                    stream=True,
                    first_chunk_timeout=5,
                    inter_chunk_timeout=30,
                )
            )
        records = [r for r in caplog.records if "工具面出站" in r.getMessage()]
        assert records, "tools 非空必须落出站观测日志"
        assert "tools=2" in records[0].getMessage()
        assert "alpha" in records[0].getMessage() and "beta" in records[0].getMessage()

    def test_mixed_invoke_and_bare_json_blocks(self) -> None:
        """多块混合：invoke 块 + 裸 JSON 块 → 全部按序解析。"""
        text = (
            "<minimax:tool_call>"
            '<invoke name="alpha"><parameter name="x">1</parameter></invoke>'
            "</minimax:tool_call>"
            "中间的话。"
            ']<]minimax[>[<tool_call>{"name": "beta", "arguments": {"y": 2}}</tool_call>'
        )
        tool_calls, _cleaned = _parse(text)
        assert [tc["name"] for tc in tool_calls] == ["alpha", "beta"]
        assert json.loads(tool_calls[0]["arguments"]) == {"x": "1"}
        assert json.loads(tool_calls[1]["arguments"]) == {"y": 2}


# ─────────────────────────── adapter 集成（LLMResponse 恢复面） ───────────────────────────


def _delta(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def _choice(delta: SimpleNamespace, finish_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def _chunk(choices: list[Any] | None = None, *, usage: Any = None) -> SimpleNamespace:
    return SimpleNamespace(choices=choices if choices is not None else [], usage=usage)


class _FakeStream:
    """按预设 chunk 序列异步迭代的桩流。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._it = iter(chunks)

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        return None


class _StubAdapter(adapter_mod._BaseLiteLLMAdapter):
    """_do_completion 返回预设流/响应并记录 kwargs 的桩适配器。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.captured: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured = dict(kwargs)
        return self._result


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestAdapterRecovery:
    def test_streaming_mangled_text_recovered(self) -> None:
        """流式聚合：正文携带倾倒形态调用块 → LLMResponse.tool_calls 解析 + 正文剥离。

        chunk 在标记中间切分（流式边界），聚合后仍须解析。
        """
        pieces = [
            "好嘞～我直接派给 general_agent 帮你执行一下～]<]minimax[>[<tool_",
            "call>\n]<]minimax[>[<invoke name=\"task_submit\">\n]<]minimax[>[<parameter name=\"goal\">执行 echo",
            " R37T02_DONE]<]minimax[>[</parameter>\n</invoke>\n]<]minimax[>[</tool_call>",
        ]
        chunks = [_chunk([_choice(_delta(content=p))]) for p in pieces]
        chunks.append(_chunk([_choice(_delta(), "stop")]))
        ad = _StubAdapter(_FakeStream(chunks))
        resp = _run(
            ad._call_streaming(
                "minimax/MiniMax-M3",
                [{"role": "user", "content": "hi"}],
                inter_chunk_timeout=30,
                first_chunk_timeout=5,
            )
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["name"] == "task_submit"
        assert "echo R37T02_DONE" in json.loads(resp.tool_calls[0]["arguments"])["goal"]
        assert resp.text is not None
        assert "minimax[>" not in resp.text
        assert "好嘞～" in resp.text

    def test_non_streaming_recovered(self) -> None:
        """非流式：content 携带规范形态调用块 → 同样恢复。"""
        message = SimpleNamespace(
            content=_CANONICAL_TEXT,
            tool_calls=None,
            reasoning_content=None,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )
        ad = _StubAdapter(response)
        resp = _run(
            ad._call_non_streaming(
                "minimax-m3",
                [{"role": "user", "content": "hi"}],
                inter_chunk_timeout=30,
            )
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["name"] == "get_weather"
        assert resp.text is not None
        assert "<minimax:tool_call>" not in resp.text

    def test_structured_tool_calls_present_skips(self) -> None:
        """结构化 tool_calls 已存在 → 不解析（不重复），正文原样保留。"""
        raw_tc = SimpleNamespace(
            id="call_existing", function=SimpleNamespace(name="f", arguments="{}")
        )
        message = SimpleNamespace(
            content="文本 <minimax:tool_call><invoke name=\"g\"></invoke></minimax:tool_call>",
            tool_calls=[raw_tc],
            reasoning_content=None,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
            usage=None,
        )
        ad = _StubAdapter(response)
        resp = _run(
            ad._call_non_streaming(
                "minimax-m3",
                [{"role": "user", "content": "hi"}],
                inter_chunk_timeout=30,
            )
        )
        assert [tc["name"] for tc in resp.tool_calls] == ["f"]
        assert "minimax:tool_call" in (resp.text or "")

    def test_non_minimax_model_gate(self) -> None:
        """非 minimax 模型 → 不解析（门禁：其他模型正文原样）。"""
        message = SimpleNamespace(
            content=_CANONICAL_TEXT,
            tool_calls=None,
            reasoning_content=None,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )
        ad = _StubAdapter(response)
        resp = _run(
            ad._call_non_streaming(
                "deepseek-v4-pro",
                [{"role": "user", "content": "hi"}],
                inter_chunk_timeout=30,
            )
        )
        assert resp.tool_calls == []
        assert resp.text == _CANONICAL_TEXT

    def test_minimax_plain_text_noop(self) -> None:
        """minimax 模型无标记正文 → 恢复面零副作用（tool_calls 空且正文原样）。"""
        message = SimpleNamespace(
            content="甲乙丙",
            tool_calls=None,
            reasoning_content=None,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )
        ad = _StubAdapter(response)
        resp = _run(
            ad._call_non_streaming(
                "minimax-m3",
                [{"role": "user", "content": "hi"}],
                inter_chunk_timeout=30,
            )
        )
        assert resp.tool_calls == []
        assert resp.text == "甲乙丙"
