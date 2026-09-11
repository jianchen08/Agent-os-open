# @feature: FP-0.2.二 内部模块 manifest | @ci: none-local
"""eval_probe 评测探针工具行为测试。

断行为不断实现：
- eval_echo：合法邮箱回显（大小写/uppercase）、非法形态拒绝并携带格式提示
  （参数错自愈链路的错误反馈契约）、非字符串防御；
- eval_sleep：短睡真实返回 slept_seconds（真实 sleep，不 mock 时钟）、
  范围外与非数字参数拒绝（性质：范围断言而非字面值）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from eval_tools import eval_broken, eval_echo, eval_sleep  # noqa: E402


class TestEvalEcho:
    """参数错 case 的错误反馈契约。"""

    @pytest.mark.parametrize(
        "query",
        [
            "zhang.san@eval-probe.local",
            "a@eval-probe.local",
            "user+tag@eval-probe.local",
        ],
    )
    def test_valid_query_echoed(self, query: str):
        result = eval_echo(query=query)
        assert result["valid"] is True
        assert result["echo"] == query

    def test_uppercase_transform(self):
        result = eval_echo(query="zhang.san@eval-probe.local", uppercase=True)
        assert result["echo"] == "ZHANG.SAN@EVAL-PROBE.LOCAL"

    @pytest.mark.parametrize(
        "query",
        [
            "zhang.san",  # 缺域名（LLM 最常见的一类参数错）
            "zhang.san@example.com",  # 域名不符
            "@eval-probe.local",  # 空 name
            "has space@eval-probe.local",  # name 含空白
            "",
        ],
    )
    def test_invalid_query_rejected_with_hint(self, query: str):
        """非法形态：valid=false + 提示里带正确形态样例（LLM 自愈依据）。"""
        result = eval_echo(query=query)
        assert result["valid"] is False
        assert "eval-probe.local" in result["message"]

    def test_non_string_query_defensive(self):
        result = eval_echo(query=123)  # type: ignore[arg-type]
        assert result["valid"] is False


class TestEvalSleep:
    """超时 case 的载体行为（真实 sleep，短时长）。"""

    def test_short_sleep_returns_slept(self):
        result = eval_sleep(seconds=0.05)
        assert result["slept_seconds"] == 0.05

    @pytest.mark.parametrize("seconds", [-1, 3601, 10**9])
    def test_out_of_range_rejected(self, seconds: float):
        """范围外拒绝（性质：任何越界值都拒绝，不限于枚举字面值）。"""
        result = eval_sleep(seconds=seconds)
        assert "error" in result

    def test_non_numeric_rejected(self):
        result = eval_sleep(seconds="30")  # type: ignore[arg-type]
        assert "error" in result

    def test_bool_rejected(self):
        """bool 是 int 子类，必须显式拒绝（True 不得当 1 秒睡）。"""
        result = eval_sleep(seconds=True)  # type: ignore[arg-type]
        assert "error" in result


def test_eval_broken_always_raises():
    """必失败探针：任何调用都抛错（tool_results 落 success=false 的故障源）。"""
    with pytest.raises(RuntimeError):
        eval_broken()
