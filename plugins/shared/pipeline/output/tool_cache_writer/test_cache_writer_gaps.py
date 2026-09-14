# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""tool_cache_writer 剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py 缺行）：

1. **spill 键消毒与 Rust 侧一致**（57）：定位符含 ``..`` 时按
   ``.. → _.`` 规则替换（与 ``spill_store::sanitize_key`` 同规则，两侧
   一致才能互读存档）——存档按消毒后路径写入时，writer 仍能回读还原全文。
2. **全特殊字符键的回退命名**（59）：键消毒后只剩 ``.``/``_``（无有效字符）
   时退化为 ``spill_<原键长度>``，长度是唯一可恢复信息；该命名同样可回读，
   且**不**从原键路径读取（两条路径可区分）。
3. **非法定位符短路**（71）：locator 无 ``/`` 分隔或缺任一侧（管道 id/键为空）
   → 直接跳过该条，不发起文件读取、不写缓存。
4. **priority 属性**（122）：默认 25（早于 track 的 15 之后、task_reminder 35
   之前——缓存写入须在提醒判定前完成）；配置值原样生效。

覆盖方式：1-3 经 ``execute`` 端到端（真实临时文件 + 真实 gzip/JSON 解码），
4 为纯属性读取。
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in (_DIR, _SHARED):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_writer_module() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "tool_cache_writer_gaps_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_writer_module()
ToolCacheWriter = _mod.ToolCacheWriter

from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402


def _cache_now() -> dict:
    from agentos_plugin_sdk import tool_result_cache as _trc

    return _trc.global_cache()


def _write_spill_file(base: Path, relative: str, payload: dict[str, Any]) -> None:
    """按 spill 布局在 base 下写入存档（gzip 压缩，与生产 spill_store 同形）。"""
    target = base / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8")))


def _ctx(executed: list[dict[str, Any]], results: list[Any]) -> PluginContext:
    return PluginContext(
        state={"_executed_tool_calls": executed, StateKeys.TOOL_RESULTS: results}
    )


def _cached_payloads() -> list[Any]:
    return [entry[0] for entry in _cache_now().values()]


def _spilled(locator: str) -> dict[str, Any]:
    return {"__spilled__": True, "tool_call_id": "call_x", "locator": locator}


def _call(query: str) -> dict[str, Any]:
    """工具调用条目（缓存键由 name + arguments 组装，参数差异区分条目）。"""
    return {"name": "web_search", "arguments": json.dumps({"query": query})}


_FULL_RESULT = {"tool_name": "web_search", "success": True, "error": None, "data": {"output": "全文"}}
_EXECUTED = [{"name": "web_search", "arguments": '{"query": "agentos"}'}]


@pytest.fixture(autouse=True)
def _isolate_cache() -> Any:
    """每例清空全局缓存（SDK 模块级单例，跨用例共享）。"""
    _cache_now().clear()
    yield
    _cache_now().clear()


# ═══════════════════════════════════════════════════════════
# 1. 键消毒（".." 替换规则）
# ═══════════════════════════════════════════════════════════


class TestKeySanitization:
    """定位符含 .. 时按消毒规则定位存档——Rust/Python 两侧同规则才能互读。"""

    @pytest.mark.parametrize(
        ("pipeline_id", "key"),
        [
            ("..x", "a..b"),               # 双侧含 ..
            ("p..q..r", "k..v..w"),        # 多次出现
            ("a..b", "plain"),             # 仅管道 id 含 ..
        ],
    )
    def test_double_dot_locator_resolves_from_sanitized_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pipeline_id: str, key: str
    ) -> None:
        """存档按 ``.. → _.`` 消毒后的路径命名 → writer 回读成功入缓存。"""
        monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
        sanitized_pid = pipeline_id.replace("..", "_.")
        sanitized_key = key.replace("..", "_.")
        assert ".." not in sanitized_pid
        assert ".." not in sanitized_key
        _write_spill_file(tmp_path, f"{sanitized_pid}/{sanitized_key}", _FULL_RESULT)

        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [_spilled(f"{pipeline_id}/{key}")])))

        assert _cached_payloads() == [_FULL_RESULT]

    def test_unsanitized_path_is_not_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：存档写在未消毒路径（a..b）→ 定位失败跳过，不写缓存。

        证明消毒是定位的必要环节，而非"任意路径都读得到"。
        """
        monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
        _write_spill_file(tmp_path, "a..b/call_full", _FULL_RESULT)

        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [_spilled("a..b/call_full")])))

        assert _cached_payloads() == []


# ═══════════════════════════════════════════════════════════
# 2. 全特殊字符键的回退命名
# ═══════════════════════════════════════════════════════════


class TestFallbackKeyNaming:
    """消毒后无有效字符 → 退化为 spill_<原键长度>，可回读且不读原键路径。"""

    @pytest.mark.parametrize("key", ["._", "__.", ".._"])
    def test_special_only_key_resolves_from_length_named_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
        _write_spill_file(tmp_path, f"pipe/spill_{len(key)}", _FULL_RESULT)

        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [_spilled(f"pipe/{key}")])))

        assert _cached_payloads() == [_FULL_RESULT]

    def test_length_name_distinguishes_same_prefix_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """性质：回退名以长度为后缀 → 不同长度键指向不同存档（长度是恢复信息）。"""
        monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
        shorter = {"tool_name": "web_search", "success": True, "error": None, "data": {"n": 1}}
        longer = {"tool_name": "web_search", "success": True, "error": None, "data": {"n": 2}}
        _write_spill_file(tmp_path, "pipe/spill_2", shorter)
        _write_spill_file(tmp_path, "pipe/spill_4", longer)

        writer = ToolCacheWriter(config={})
        asyncio.run(
            writer.execute(_ctx([_call("q1")], [_spilled("pipe/._")]))        # len 2
        )
        asyncio.run(
            writer.execute(_ctx([_call("q2")], [_spilled("pipe/__..")]))      # len 4
        )

        assert _cached_payloads() == [shorter, longer]

    def test_original_key_path_is_not_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：存档写在原键路径（._）→ 定位失败；回退名路径才是读取面。"""
        monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
        _write_spill_file(tmp_path, "pipe/._", _FULL_RESULT)

        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [_spilled("pipe/._")])))

        assert _cached_payloads() == []


# ═══════════════════════════════════════════════════════════
# 3. 非法定位符短路
# ═══════════════════════════════════════════════════════════


class TestInvalidLocatorShortCircuit:
    """locator 结构非法 → 跳过该条（不读盘、不写缓存）。"""

    @pytest.mark.parametrize(
        "locator",
        [
            "no-slash-at-all",   # 无分隔符
            "/key-only",         # 管道 id 为空
            "pipe-only/",        # 键为空
            "",                  # 空定位符
        ],
    )
    def test_invalid_locator_skipped(self, locator: str) -> None:
        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [_spilled(locator)])))
        assert _cached_payloads() == []

    def test_missing_locator_key_skipped(self) -> None:
        """定位符键缺失等同空串（同短路语义）。"""
        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [{"__spilled__": True}])))
        assert _cached_payloads() == []

    def test_valid_locator_neighbour_still_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：同批中非法定位符被跳过的同时，合法条目照常入缓存（跳过 ≠ 中断）。"""
        monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
        _write_spill_file(tmp_path, "pipe/call_full", _FULL_RESULT)

        writer = ToolCacheWriter(config={})
        asyncio.run(
            writer.execute(
                _ctx(
                    _EXECUTED + [{"name": "web_search", "arguments": "{}"}],
                    [_spilled("no-slash"), _spilled("pipe/call_full")],
                )
            )
        )
        assert _cached_payloads() == [_FULL_RESULT]

    def test_plain_result_without_spill_marker_unaffected(self) -> None:
        """非定位符的普通成功结果不受短路影响（直通入缓存）。"""
        writer = ToolCacheWriter(config={})
        asyncio.run(writer.execute(_ctx(_EXECUTED, [_FULL_RESULT])))
        assert _cached_payloads() == [_FULL_RESULT]


# ═══════════════════════════════════════════════════════════
# 4. priority 属性
# ═══════════════════════════════════════════════════════════


class TestPriority:
    @pytest.mark.parametrize("configured", [0, 5, 60])
    def test_priority_honours_config(self, configured: int) -> None:
        assert ToolCacheWriter(config={"priority": configured}).priority == configured

    def test_priority_defaults_to_25(self) -> None:
        """缺省 25：早于 task_reminder(35)（缓存写入须先于提醒判定完成）。"""
        assert ToolCacheWriter().priority == 25
        assert ToolCacheWriter(config={}).priority == 25

    def test_name_stable(self) -> None:
        assert ToolCacheWriter().name == "tool_cache_writer"


# ═══════════════════════════════════════════════════════════
# 附：命名空间与身份隔离在缺口路径上同样生效
# ═══════════════════════════════════════════════════════════


def test_namespace_applied_on_resolved_spill_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定位符回读入缓存的条目按 state 身份键隔离（与直通路径同规则）。

    断言手段：同一调用在**有无身份键**两种 state 下产出不同缓存键——身份参与
    键组装是可观察行为；单键存在数不说明隔离，故不做键名猜测。
    """
    monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path))
    _write_spill_file(tmp_path, "pipe/call_full", _FULL_RESULT)

    writer = ToolCacheWriter(config={})
    identity = {"pipeline_id": "pipe", "session_id": "s1"}
    call = _call("q-identity")

    def _write(state_extra: dict[str, Any]) -> set[str]:
        _cache_now().clear()
        ctx = PluginContext(
            state={
                "_executed_tool_calls": [call],
                StateKeys.TOOL_RESULTS: [_spilled("pipe/call_full")],
                **state_extra,
            }
        )
        asyncio.run(writer.execute(ctx))
        assert len(_cache_now()) == 1, "回读成功 → 恒有且仅有一条入缓存"
        return set(_cache_now())

    with_identity = _write(identity)
    without_identity = _write({})

    assert with_identity != without_identity, "身份键必须参与缓存键（跨身份不可互命）"
    assert _write(identity) == with_identity, "同一身份重复写入键稳定（幂等）"
