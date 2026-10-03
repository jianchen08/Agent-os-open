# @feature: FP-T12 前端适配 | @ci: python-coverage
"""resolve_thinking_params 消息级思考参数覆盖解析测试。

thinking_strength 线上形态 = 参数组 JSON 串（llm_service thinking-levels
端点下发的选项 value，选中即透传）：本侧只做 JSON 解析 + 思考参数白名单
过滤，零档位查表。
1. 合法参数组 JSON → 原样透出（键在白名单内）；
2. 白名单外键（temperature/max_tokens 等采样参数）过滤——采样参数不随消息覆盖；
3. 全键被过滤 → None（不覆盖）；
4. 空 / 非 JSON / 非对象 JSON → None（不覆盖，不抛）。
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


@pytest.fixture
def rtp() -> Any:
    """按显式路径加载 llm_core plugin.py（裸名防劫持），取解析函数。"""
    spec = importlib.util.spec_from_file_location(
        "llm_core_plugin_thinking_test", str(_DIR / "plugin.py")
    )
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_core_plugin_thinking_test"] = m
    spec.loader.exec_module(m)
    return m.resolve_thinking_params


def _json(params: Any) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_valid_params_pass_through(rtp: Any) -> None:
    params = {"reasoning_effort": "max", "thinking": {"type": "enabled"}}
    assert rtp(_json(params)) == params


def test_whitelist_filters_sampling_params(rtp: Any) -> None:
    """采样参数（temperature/max_tokens）在参数组里也过滤，只留思考键。"""
    raw = _json({"reasoning_effort": "low", "temperature": 0.9, "max_tokens": 100})
    assert rtp(raw) == {"reasoning_effort": "low"}


def test_all_filtered_returns_none(rtp: Any) -> None:
    assert rtp(_json({"temperature": 0.5})) is None


def test_empty_or_invalid_returns_none(rtp: Any) -> None:
    assert rtp("") is None
    assert rtp("not-json") is None
    # 旧档位词汇（键名裸串）不是 JSON 对象 → 不覆盖
    assert rtp("high") is None
    assert rtp(_json(["reasoning_effort", "max"])) is None
    assert rtp(_json({"reasoning_effort": "low"})) == {"reasoning_effort": "low"}
