# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""tool_cache（input 读缓存）缺口分支补测（coverage.xml 缺口行靶单）。

行为契约（断输入→输出/副作用，不 mock 插件内部协作）：
- priority：config 覆盖，缺省 35（schema 校验之后的位置契约）。
- 缓存停用（enabled=False）→ 零产出（不查缓存、不动 raw_tool_calls）。
- 无原始工具调用（缺键/空列表/None）→ 零产出（普通 LLM 轮不受影响）。
- 被排除工具（有副作用）→ 零产出（结果不可复用，绝不短路执行）。
- 全命中 → cache_hit=True + tool_results 回填 + raw_tool_calls 清空 +
  messages 补 role=tool 配对（防死循环）且 skip_remaining=True。
- 未命中 / 混合命中 → 零产出（任一未命中即整体执行，不部分短路）。
- 身份隔离：pipeline/session/user 不同的同参调用互不命中（ns 维度）。
- put 写入经 SDK 共享单例，execute 查询命中同一条目（input 与
  tool_cache_writer 共享同一缓存字典的真往返）。

缓存实现（agentos_plugin_sdk.tool_result_cache）是真依赖，只对
「结果不可 JSON 序列化」的边界注入不可序列化对象（外部数据形态）。

守卫分支（靶行 plugin.py 170）逐条说明：
- ``_build_tool_result_messages`` 的 ``if i >= len(cached_results): break``：
  ``execute`` 只在**全部**工具调用命中时才调用本方法（任一未命中即
  ``return PluginResult()``），且每命中一次恰好 append 一条结果，故
  ``len(cached_results) == len(tool_calls)`` 恒成立，经 ``execute`` 的真实
  输入不可达——它是防未来"部分命中"改动的防御性护栏。护栏行为按其本义以
  直调方法注入长度差输入覆盖（TestPartialCacheGuardBranch），不硬凑
  execute 路径。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from agentos_plugin_sdk.tool_result_cache import global_cache

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared" / "pipeline" / "input" / "tool_cache"
_SHARED_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared"
for _d in (str(_PLUGIN_DIR), str(_SHARED_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 裸名 `plugin` 在同进程多插件测试共跑时会被其它插件目录的 plugin.py 抢注
# （车道内既有先例）：按显式路径加载本插件模块，独享实例。
import importlib.util  # noqa: E402

_tc_spec = importlib.util.spec_from_file_location(
    "tool_cache_plugin_gaps_under_test", str(_PLUGIN_DIR / "plugin.py")
)
assert _tc_spec is not None and _tc_spec.loader is not None
tc_plugin = importlib.util.module_from_spec(_tc_spec)
sys.modules["tool_cache_plugin_gaps_under_test"] = tc_plugin
_tc_spec.loader.exec_module(tc_plugin)

from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_shared_cache() -> Any:
    """每条用例独占共享单例缓存（input/output 两端共用的进程级字典）。"""
    cache = global_cache()
    cache.clear()
    yield
    cache.clear()


def _run(p: Any, state: dict[str, Any]) -> Any:
    return asyncio.run(p.execute(PluginContext(state=state, config={})))


def _state(tool: str = "web_search", args: dict[str, Any] | None = None,
           **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        StateKeys.RAW_TOOL_CALLS: [
            {"id": "call_1", "name": tool, "args": args if args is not None else {"q": "x"}},
        ],
    }
    base.update(extra)
    return base


# ═══════════════ 身份与配置 ═══════════════


class TestIdentityAndConfig:
    def test_name_and_default_priority(self) -> None:
        """name 恒为插件 id；priority 缺省 35（schema 校验之后）。"""
        p = tc_plugin.ToolCache()

        assert p.name == "tool_cache"
        assert p.priority == 35
        assert tc_plugin.ToolCache(config={"priority": 7}).priority == 7


# ═══════════════ 早退族 ═══════════════


class TestEarlyReturns:
    @pytest.mark.parametrize(
        ("raw_calls", "label"),
        [
            (None, "None"),
            ([], "empty-list"),
            ((), "empty-tuple"),
        ],
    )
    def test_no_raw_tool_calls_yields_no_output(self, raw_calls: Any, label: str) -> None:
        """无原始工具调用（None/空序列）→ 零产出（普通 LLM 轮零副作用）。"""
        p = tc_plugin.ToolCache()
        state = {StateKeys.RAW_TOOL_CALLS: raw_calls}

        result = _run(p, state)

        assert result.state_updates == {}, f"{label} 不得产生任何 state 写入"

    def test_disabled_cache_skips_query_even_on_hit(self) -> None:
        """enabled=False → 即使缓存里有条目也零产出（读面整体停用）。"""
        seed = tc_plugin.ToolCache()
        seed.put({"name": "web_search", "args": {"q": "x"}}, {"answer": 42})

        p = tc_plugin.ToolCache(config={"enabled": False})
        result = _run(p, _state())

        assert result.state_updates == {}
        assert result.skip_remaining is False

    @pytest.mark.parametrize("tool", ["bash_execute", "file_write", "task_submit"])
    def test_excluded_tools_never_bypass_execution(self, tool: str) -> None:
        """默认排除表内的有副作用工具：即便缓存有同键条目也不短路（结果不可复用）。"""
        p = tc_plugin.ToolCache()
        tool_call = {"id": "c1", "name": tool, "args": {"a": 1}}
        p.put(tool_call, {"side_effect": True})

        result = _run(p, _state(tool=tool, args={"a": 1}))

        assert result.state_updates == {}
        assert result.skip_remaining is False

    def test_custom_exclude_tools_config(self) -> None:
        """exclude_tools 配置可扩表：新列入的工具同样跳过缓存查询。"""
        p = tc_plugin.ToolCache(config={"exclude_tools": ["web_search"]})
        p.put({"name": "web_search", "args": {"q": "x"}}, {"cached": True})

        assert _run(p, _state()).state_updates == {}


class TestMissReturnsNoOutput:
    @pytest.mark.parametrize(
        ("cached_args", "queried_args"),
        [
            ({"q": "different"}, {"q": "x"}),        # 同工具不同参数
            ({"q": "x", "n": 5}, {"q": "x"}),         # 参数子集不命中
        ],
    )
    def test_arg_mismatch_misses(self, cached_args: dict[str, Any],
                                 queried_args: dict[str, Any]) -> None:
        """参数不同的同工具调用不命中 → 零产出（必须真实执行）。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": cached_args}, {"cached": True})

        assert _run(p, _state(args=queried_args)).state_updates == {}

    def test_mixed_hit_and_miss_yields_no_output(self) -> None:
        """多调用混合命中 → 零产出（任一未命中即整体执行，不做部分短路）。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": {"q": "hit"}}, {"cached": True})
        state = {
            StateKeys.RAW_TOOL_CALLS: [
                {"id": "c1", "name": "web_search", "args": {"q": "hit"}},
                {"id": "c2", "name": "web_search", "args": {"q": "miss"}},
            ],
        }

        result = _run(p, state)

        assert result.state_updates == {}
        assert result.skip_remaining is False


# ═══════════════ 命中面 ═══════════════


class TestHitShortCircuit:
    def test_single_hit_writes_short_circuit_updates(self) -> None:
        """命中 → cache_hit=True、tool_results 回填、raw_tool_calls 清空、
        skip_remaining=True（跳过后续插件与工具执行）。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": {"q": "x"}}, {"answer": 42})

        result = _run(p, _state())

        updates = result.state_updates
        assert updates["cache_hit"] is True
        assert updates[StateKeys.TOOL_RESULTS] == [{"answer": 42}]
        assert updates[StateKeys.RAW_TOOL_CALLS] == []
        assert result.skip_remaining is True

    def test_hit_builds_tool_result_messages_paired_with_calls(self) -> None:
        """messages 补 role=tool 配对消息（llm_core 已 append assistant(tool_calls)）：
        否则 LLM 下一轮看不到结果会重发同一调用，再命中形成死循环。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": {"q": "x"}}, {"answer": 42})

        result = _run(p, _state())

        ops = result.state_updates["messages"]["_ops"]
        assert len(ops) == 1
        op = ops[0]
        assert op["op"] == "set" and "seq" not in op, "无 seq = append 语义"
        msg = op["msg"]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_1"
        assert msg["tool_result"]["tool_name"] == "web_search"
        assert msg["tool_result"]["data"] == {"answer": 42}

    def test_hit_message_content_is_json_text(self) -> None:
        """非字符串结果经 JSON 序列化为 content（对齐 tool_core 序列化形状）。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": {"q": "x"}}, {"nested": [1, 2]})

        ops = _run(p, _state()).state_updates["messages"]["_ops"]

        assert ops[0]["msg"]["content"] == '{"nested": [1, 2]}'

    def test_string_result_kept_verbatim(self) -> None:
        """字符串结果原样进 content，tool_result.data 包 content 键（形态契约）。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": {"q": "x"}}, "plain text")

        msg = _run(p, _state()).state_updates["messages"]["_ops"][0]["msg"]

        assert msg["content"] == "plain text"
        assert msg["tool_result"]["data"] == {"content": "plain text"}

    def test_unserializable_result_falls_back_to_str(self) -> None:
        """结果不可 JSON 序列化（自定义对象）→ content 经 default=str 序列化，
        不抛（读面留可读痕迹）。"""
        p = tc_plugin.ToolCache()

        class _Opaque:
            def __repr__(self) -> str:
                return "<opaque>"

        p.put({"name": "web_search", "args": {"q": "x"}}, _Opaque())

        msg = _run(p, _state()).state_updates["messages"]["_ops"][0]["msg"]

        assert isinstance(msg["content"], str)
        assert json.loads(msg["content"]) == "<opaque>"

    def test_circular_result_falls_back_to_str(self) -> None:
        """结果含循环引用（JSON 无法编码）→ content 回落 str(result)（TypeError/
        ValueError 都被接住，插件读面不因工具数据形态而崩）。"""
        p = tc_plugin.ToolCache()
        cyclic: list[Any] = []
        cyclic.append(cyclic)
        p.put({"name": "web_search", "args": {"q": "x"}}, cyclic)

        msg = _run(p, _state()).state_updates["messages"]["_ops"][0]["msg"]

        assert msg["content"] == str(cyclic)
        assert msg["content"].startswith("[["), "回落文本是 Python repr 而非 JSON"

    def test_multi_hit_results_paired_in_order(self) -> None:
        """多调用全命中：结果与 tool_calls 按下标配对，每个调用一条配对消息。"""
        p = tc_plugin.ToolCache()
        for i, q in enumerate(("a", "b", "c")):
            p.put({"name": "web_search", "args": {"q": q}}, {"idx": i})
        state = {
            StateKeys.RAW_TOOL_CALLS: [
                {"id": f"c{i}", "name": "web_search", "args": {"q": q}}
                for i, q in enumerate(("a", "b", "c"))
            ],
        }

        result = _run(p, state)

        assert result.state_updates[StateKeys.TOOL_RESULTS] == [
            {"idx": 0}, {"idx": 1}, {"idx": 2},
        ]
        ops = result.state_updates["messages"]["_ops"]
        assert [op["msg"]["tool_call_id"] for op in ops] == ["c0", "c1", "c2"]
        assert [op["msg"]["tool_result"]["data"] for op in ops] == [
            {"idx": 0}, {"idx": 1}, {"idx": 2},
        ]

    def test_missing_call_id_gets_synthetic_id(self) -> None:
        """调用缺 id → 合成 call_{index}（配对消息仍可寻址，不产生 None 键）。"""
        p = tc_plugin.ToolCache()
        p.put({"name": "web_search", "args": {"q": "x"}}, {"ok": True})
        state = {StateKeys.RAW_TOOL_CALLS: [{"name": "web_search", "args": {"q": "x"}}]}

        msg = _run(p, state).state_updates["messages"]["_ops"][0]["msg"]

        assert msg["tool_call_id"] == "call_0"
        assert msg["tool_result"]["call_id"] == "call_0"

    @pytest.mark.parametrize(
        ("written_identity", "queried_identity"),
        [
            ({"pipeline_id": "p1"}, {"pipeline_id": "p2"}),
            ({"session_id": "s1"}, {"session_id": "s2"}),
            ({"user_id": "u1"}, {"user_id": "u2"}),
            (None, {"pipeline_id": "p1"}),              # 无身份条目 vs 带身份查询
        ],
    )
    def test_identity_dimension_prevents_cross_identity_hit(
        self, written_identity: dict[str, str] | None,
        queried_identity: dict[str, str],
    ) -> None:
        """身份键（pipeline/session/user）不同的同参调用互不命中（私有数据不串）。"""
        from agentos_plugin_sdk.tool_result_cache import ToolResultCache, namespace_from_state

        writer = ToolResultCache()  # 与插件同一 SDK 真值源（output 写端语义）
        tool_call = {"name": "memory_search", "args": {"q": "private"}}
        writer.put(
            tool_call,
            {"secret": True},
            namespace=namespace_from_state(written_identity) if written_identity else None,
        )

        result = _run(
            tc_plugin.ToolCache(),
            _state(tool="memory_search", args={"q": "private"}, **queried_identity),
        )

        assert result.state_updates == {}, "跨身份查询必须 miss"

    @pytest.mark.parametrize(
        "identity",
        [{"pipeline_id": "p-1"}, {"session_id": "s-1"}, {"user_id": "u-1"}],
    )
    def test_same_identity_round_trips(self, identity: dict[str, str]) -> None:
        """同一身份写入/查询往返命中（隔离不破坏正常缓存命中）。"""
        from agentos_plugin_sdk.tool_result_cache import ToolResultCache, namespace_from_state

        ToolResultCache().put(
            {"name": "memory_search", "args": {"q": "private"}},
            {"secret": "same-identity"},
            namespace=namespace_from_state(identity),
        )

        result = _run(
            tc_plugin.ToolCache(),
            _state(tool="memory_search", args={"q": "private"}, **identity),
        )

        assert result.state_updates["cache_hit"] is True
        assert result.state_updates[StateKeys.TOOL_RESULTS] == [{"secret": "same-identity"}]


class TestPutRoundTrip:
    def test_put_then_execute_hits_shared_singleton(self) -> None:
        """put 与 execute 经同一 SDK 共享单例：写入即读命中（两端同源真往返）。"""
        p = tc_plugin.ToolCache()
        tool_call = {"id": "c9", "name": "web_search", "args": {"q": "shared"}}

        p.put(tool_call, {"from_writer": True})
        result = _run(p, _state(args={"q": "shared"}))

        assert result.state_updates["cache_hit"] is True
        assert result.state_updates[StateKeys.TOOL_RESULTS] == [{"from_writer": True}]

    def test_cache_entry_survives_separate_plugin_instances(self) -> None:
        """跨插件实例（input 读 / output 写各自的 ToolCache 对象）共用同一条目。"""
        writer = tc_plugin.ToolCache()
        reader = tc_plugin.ToolCache()

        writer.put({"name": "web_search", "args": {"q": "x"}}, {"v": 1})

        assert _run(reader, _state()).state_updates["cache_hit"] is True


# ═══════════════ 越界守卫（靶行 170：长度差直调注入） ═══════════════


class TestPartialCacheGuardBranch:
    """``_build_tool_result_messages`` 的 ``if i >= len(cached_results): break``。

    经 ``execute`` 的真实输入不可达（见模块 docstring：全命中才调用、命中数
    恒等于调用数），这里按守卫本义直调方法、注入长度差输入验证护栏契约：
    ops 数 = min(调用数, 缓存数)，多余调用不产生 op、不越界不抛错。
    """

    @staticmethod
    def _calls(n: int) -> list[dict[str, Any]]:
        return [
            {"id": f"c{i}", "name": "web_search", "args": {"q": f"q{i}"}}
            for i in range(n)
        ]

    @pytest.mark.parametrize(
        ("n_calls", "n_cached"),
        [
            (3, 2),  # 靶形态：第三个调用越过缓存末尾 → break
            (3, 0),  # 零缓存：首个调用即越界
            (3, 3),  # 对照：等长全回填（execute 真实契约形态）
            (1, 5),  # 反向量级：缓存多于调用，多余缓存被忽略
        ],
    )
    def test_ops_count_is_min_of_calls_and_cached(
        self, n_calls: int, n_cached: int
    ) -> None:
        p = tc_plugin.ToolCache()
        ctx = PluginContext(state={}, config={})
        cached = [{"idx": i} for i in range(n_cached)]

        ops = p._build_tool_result_messages(ctx, self._calls(n_calls), cached)["_ops"]

        assert len(ops) == min(n_calls, n_cached), "ops 数必须按 min 截断（守卫语义）"
        assert [op["msg"]["tool_call_id"] for op in ops] == [
            f"c{i}" for i in range(min(n_calls, n_cached))
        ], "截断后的 ops 必须与前三（或全部）调用按下标配对"
