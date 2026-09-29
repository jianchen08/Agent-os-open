# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core tool_call id 单一关联键测试（ADR 2026-09-28-tool-call-live-card）。

契约：provider 返回的 id 原样透传——流式增量事件（llm_service 逐 chunk 发
原始 id）、tool_start/tool_result 事件、assistant 消息、raw_tool_calls、持久
化与前端渲染全程同一个 id。仅缺失/空 id 兜底生成 call_<hex>。

历史反例（本测试锁死不复归）：按内部 call_<hex> 白名单重造非空 id，会让流
事件（原始 id）与最终结果（重造 id）分叉——前端同一工具调用渲染两张卡
（装机版 MiniMax call-<uuid> 连字符形态实测，一天 89 条重造日志）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
_CORE_DIR = _LLM_CORE_DIR.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"

# 平铺 import 路径与 test_llm_core_execute_paths.py 同款（adapter.py 双目录
# 压序说明见彼处）。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

_MOD_NAME = "llm_core_tool_call_ids_under_test"


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


_FABRICATED_ID_RE = re.compile(r"call_[0-9a-f]{24}\Z")


@pytest.fixture
def core() -> Any:
    return _load_plugin().LLMCore({})


class TestResolveToolCallIds:
    def test_provider_ids_passthrough(self, core) -> None:
        """非 call_<hex> 形态的 provider id 原样保留（不重造）——双卡事故根因锚。"""
        provider_ids = [
            # MiniMax 实测形态（call-<uuid> 连字符）
            "call-65c4f72b-b49a-425f-bebf-cc1231a3397b",
            # gemini/litellm 函数式命名形态
            "call_function_read_1",
            # 标准 call_<hex> 形态（OpenAI 系）
            "call_11aab432c8384093a613e0a1",
        ]
        tool_calls = [{"id": cid} for cid in provider_ids]

        resolved = core._resolve_tool_call_ids(tool_calls)

        assert resolved == provider_ids
        assert [tc["id"] for tc in tool_calls] == provider_ids

    def test_missing_or_empty_id_fabricated(self, core) -> None:
        """缺失/空 id 兜底生成 call_<hex24>，批内互异（并行调用可区分）。"""
        tool_calls = [{"id": None}, {"id": ""}, {}]

        resolved = core._resolve_tool_call_ids(tool_calls)

        for rid in resolved:
            assert _FABRICATED_ID_RE.fullmatch(rid), f"兜底 id 形态非法: {rid}"
        # 性质断言：批内唯一（两条缺失 id 不得撞同一兜底值）
        assert len(set(resolved)) == 3

    def test_in_place_writeback_order_preserved(self, core) -> None:
        """混合批（provider id + 缺失 id）：返回序与原地回写一一对应。"""
        tool_calls: list[dict[str, str | None]] = [
            {"id": "call-aaa-bbb"},
            {"id": None},
            {"id": "call_function_x_2"},
        ]

        resolved = core._resolve_tool_call_ids(tool_calls)

        # 性质断言：resolved[i] 与回写后的 tool_calls[i]["id"] 恒同值
        assert resolved == [tc["id"] for tc in tool_calls]
        assert resolved[0] == "call-aaa-bbb"
        assert resolved[2] == "call_function_x_2"
