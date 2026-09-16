# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""Python 簇 H 缺口补测（cost_control 观测族）——coverage.xml 2026-09-14 口径缺行。

靶单：
- server.py 375-379（traces 真值聚合命中：真库连接 + 日/月求和）、381-383
  （finally 关闭连接 + sqlite 故障保留内存账本）；
- exceptions.py 50,52-54（_default_code 类名归一：去 Exception 后缀大写）、
  62（__str__ 信封形态）；
- tokenizer.py 41（构造期 KeyError 回退 encoding_for_model）、91-92
  （count_messages 取编码器 KeyError 包装为 ValueError）。

不可达行说明（逐条）：
- tokenizer.py 41 与 91-92 的 except 支在 tiktoken 0.13 下**不可达**——该版本
  对未知编码名抛 ValueError（非 KeyError），仓库现有测试也如实断言了这一事实
  （tests/plugins/system/cost_control/test_tokenizer.py::test_invalid_encoding_name_fallback_unreachable）。
  本文件用 monkeypatch 把外部依赖 tiktoken.get_encoding 改成抛 KeyError，恢复
  "依赖升级/降级到抛 KeyError 的版本"时的分支语义，使该防御分支保持可执行、
  可回归——这是外部依赖替身（非内部 mock），符合测试纪律。

外部依赖替身：sqlite 走真实临时库（含真库 / 非数据库文件两种形态）；BudgetManager
走真实实现（on_load 装配 + record_usage 记账）；tiktoken 按上条处理。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import importlib.util
import json
import logging
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import tiktoken

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/cost_control/
while str(_PLUGIN_DIR) in sys.path:
    sys.path.remove(str(_PLUGIN_DIR))
sys.path.insert(0, str(_PLUGIN_DIR))

# 裸名逐出：exceptions/budget_manager/config 等平铺名遍布各插件（llm 有同名
# exceptions.py），收集期被别的插件占位会让本文件的平铺 import 打到错误实现。
for _bare in ("exceptions", "budget_manager", "tokenizer", "config"):
    sys.modules.pop(_bare, None)

from budget_manager import reset_budget_manager  # noqa: E402
from exceptions import BaseAppException, CostControlException  # noqa: E402
from tokenizer import TokenCounter  # noqa: E402


def _load_server() -> Any:
    """动态加载 cost_control/server.py（唯一模块名，隔离模块级单例）。"""
    mod_name = "cost_control_cluster_h_server"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


server_mod = _load_server()


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    assert result["success"], result
    resp = result["data"]
    return resp["status"], json.loads(base64.b64decode(resp["body"]))


@pytest.fixture
def cost_server() -> Any:
    """真实 BudgetManager 装配（on_load 公共入口）+ 用例后复位全局单例。"""
    _run(server_mod._on_load({}))
    try:
        yield server_mod
    finally:
        server_mod._budget_manager = None
        reset_budget_manager()


def _seed_traces_db(path: Path, total_tokens: int) -> None:
    """真 sqlite 库：一行当日 llm_usage trace（列形态对齐内核 traces 表）。"""
    now = datetime.datetime.now(datetime.UTC)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT, created_at TEXT)")
        conn.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?)",
            (
                "t-1",
                "core",
                json.dumps({"llm_usage": {"input_tokens": total_tokens - 100, "output_tokens": 100,
                                          "total_tokens": total_tokens}}),
                now.isoformat().replace("+00:00", "Z"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# server.py：_reshape_usage_statistics 的 traces 真值聚合与故障降级
# ═══════════════════════════════════════════════════════════


class TestUsageStatisticsTracesAggregation:
    """usage/statistics 端点的 traces 消耗真值优先与降级契约。"""

    def test_traces_totals_override_manager_ledger(
        self, cost_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """375-379,381：真库存在时消耗字段取 traces 求和，覆盖 manager 内存账本。

        区分度：manager 账本记 100，traces 记 1500——响应必须是 1500，
        证明走的是 DB 聚合而非内存值。
        """
        _run(cost_server.cost_control_record_usage(tokens=100, model="m-1"))
        db = tmp_path / "kernel.db"
        _seed_traces_db(db, 1500)
        monkeypatch.setattr(cost_server, "kernel_db_path", lambda: db)

        status, body = _decode(
            _run(cost_server.http_handle(path="/ext/cost_control/usage/statistics", method="GET"))
        )
        assert status == 200
        stats = body["global_stats"]
        assert stats["daily_tokens"] == 1500
        assert stats["monthly_tokens"] == 1500
        # 性质断言：当月窗口包含当日 → monthly >= daily；占比随限额派生
        assert stats["monthly_tokens"] >= stats["daily_tokens"]
        assert stats["daily_usage_percent"] > 0

    def test_daily_cost_scales_with_traces_tokens(
        self, cost_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """378-379 性质：日成本与 traces 消耗线性同向（费率取自 manager 账本比值）。

        两组输入 1500/3000 → 成本比值恒等于 token 比值（不依赖具体费率数值）。
        """
        _run(cost_server.cost_control_record_usage(tokens=100, model="m-1"))

        costs: list[float] = []
        for tokens in (1500, 3000):
            db = tmp_path / f"kernel_{tokens}.db"
            _seed_traces_db(db, tokens)
            monkeypatch.setattr(cost_server, "kernel_db_path", lambda p=db: p)
            _, body = _decode(
                _run(cost_server.http_handle(path="/ext/cost_control/usage/statistics", method="GET"))
            )
            assert body["global_stats"]["daily_tokens"] == tokens
            costs.append(body["global_stats"]["estimated_daily_cost"])

        assert costs[0] > 0
        assert costs[1] == pytest.approx(costs[0] * 2, rel=1e-9)

    def test_corrupt_db_falls_back_to_manager_ledger(
        self, cost_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """382-383：库文件存在但非 sqlite（查询抛 sqlite3.Error）→ 保留内存账本 + 留痕。"""
        _run(cost_server.cost_control_record_usage(tokens=250, model="m-1"))
        bogus = tmp_path / "not_a_db.db"
        bogus.write_text("this is definitely not a sqlite database", encoding="utf-8")
        monkeypatch.setattr(cost_server, "kernel_db_path", lambda: bogus)

        with caplog.at_level(logging.WARNING, logger="cost_control.server"):
            status, body = _decode(
                _run(cost_server.http_handle(path="/ext/cost_control/usage/statistics", method="GET"))
            )

        assert status == 200
        assert body["global_stats"]["daily_tokens"] == 250  # manager 账本原值
        assert any("traces 消耗聚合失败" in r.message for r in caplog.records)

    def test_missing_db_file_keeps_manager_ledger(
        self, cost_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """对照支：库文件不存在 → 不进 DB 分支，账本值原样（与故障降级同一出口）。"""
        _run(cost_server.cost_control_record_usage(tokens=77, model="m-1"))
        monkeypatch.setattr(cost_server, "kernel_db_path", lambda: tmp_path / "absent.db")
        _, body = _decode(
            _run(cost_server.http_handle(path="/ext/cost_control/usage/statistics", method="GET"))
        )
        assert body["global_stats"]["daily_tokens"] == 77


# ═══════════════════════════════════════════════════════════
# exceptions.py：默认错误码派生与字符串信封
# ═══════════════════════════════════════════════════════════


class _SuffixlessFault(BaseAppException):
    """类名不带 Exception 后缀的异常子类（对照 _default_code 的 False 支）。"""


class _LedgerFaultException(BaseAppException):
    """另一档带 Exception 后缀的异常子类（第二组区分度输入）。"""


class TestDefaultCodeDerivation:
    """未显式给 code 时按类名派生：去 Exception 后缀 + 大写。"""

    @pytest.mark.parametrize(
        ("exc", "expected_code"),
        [
            (CostControlException("超预算"), "COSTCONTROL"),
            (_LedgerFaultException("账本异常"), "_LEDGERFAULT"),
            (_SuffixlessFault("无后缀"), "_SUFFIXLESSFAULT"),
        ],
        ids=["with-suffix", "other-suffix", "without-suffix"],
    )
    def test_code_stripped_and_uppercased(self, exc: BaseAppException, expected_code: str) -> None:
        """50,52-54：带 Exception 后缀去后缀（COSTCONTROL），不带后缀仅大写。

        两组区分度输入：类名以 Exception 结尾 / 不以它结尾（后者证明 52 的
        否分支同样成立，不是"恒去后缀"）。
        """
        assert exc.code == expected_code
        assert not exc.code.endswith("EXCEPTION")

    @pytest.mark.parametrize("code", [None, "", "CUSTOM_CODE"])
    def test_explicit_code_wins_over_derivation(self, code: str | None) -> None:
        """性质断言：显式 code 原样保留（空/None 才回退派生），派生值恒为大写字母数字。"""
        exc = CostControlException("m", code=code)
        if code:
            assert exc.code == code
        else:
            assert exc.code == "COSTCONTROL"
        assert exc.code == exc.code.upper()

    def test_str_renders_code_and_message_envelope(self) -> None:
        """62：__str__ 形态 ``[CODE] message``（日志/前端提示的公共契约）。"""
        exc = CostControlException("磁盘写入失败")
        assert str(exc) == "[COSTCONTROL] 磁盘写入失败"
        assert repr(exc).startswith("CostControlException(message=")
        assert exc.to_dict()["code"] == "COSTCONTROL"


# ═══════════════════════════════════════════════════════════
# tokenizer.py：编码器回退与 KeyError 包装（外部依赖替身）
# ═══════════════════════════════════════════════════════════


class TestTokenizerEncodingKeyErrorBranches:
    """tiktoken 抛 KeyError 版本下的两条防御分支（见模块 docstring 不可达说明）。"""

    def test_constructor_falls_back_to_gpt4_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """41：构造期 get_encoding 抛 KeyError → 回退 encoding_for_model("gpt-4")。

        断言可观察行为：实例可用且编码名与 gpt-4 的编码一致（cl100k_base）。
        """
        def _raising(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(tiktoken, "get_encoding", _raising)
        counter = TokenCounter("some-unknown-encoding")
        assert counter.encoding.name == tiktoken.encoding_for_model("gpt-4").name
        assert counter.count_tokens("hello") > 0

    def test_count_messages_wraps_key_error_as_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """91-92：取编码器抛 KeyError → 包装为 ValueError（含编码名，fail-visible）。"""
        def _raising(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(tiktoken, "get_encoding", _raising)
        counter = TokenCounter()
        with pytest.raises(ValueError, match="无法获取编码器 cl100k_base") as excinfo:
            counter.count_messages([{"role": "user", "content": "hi"}], model="gpt-4")
        assert isinstance(excinfo.value.__cause__, KeyError)

    def test_healthy_dependency_matches_real_encoder(self) -> None:
        """对照支（真实依赖）：未替身时计数与 tiktoken 真值一致，包装分支不触发。"""
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "你好世界"},
            {"role": "assistant", "content": "ok"},
        ]
        encoding = tiktoken.get_encoding("cl100k_base")
        expected = len(messages) * 4 + 3  # 每条消息开销 + 回复辅助开销
        for message in messages:
            expected += len(encoding.encode(message["role"]))
            expected += len(encoding.encode(message["content"]))
            expected += 4  # "role" + ": " 与 "content" + ": " 两档字段名开销
        total = counter.count_messages(messages, model="gpt-4")
        assert total == expected
        assert total > len(messages) * 4  # 性质：含内容必高于纯开销下界

    def test_unsupported_model_still_raises_value_error(self) -> None:
        """守卫支：模型前缀不匹配 → ValueError（在取编码器之前拦截，不进 91-92）。"""
        counter = TokenCounter()
        with pytest.raises(ValueError, match="不支持的模型名称"):
            counter.count_messages([{"role": "user", "content": "x"}], model="mystery-model")


@pytest.mark.parametrize("suffix", ["Exception", "Fault"])
def test_default_code_keeps_public_surface_for_plain_types(suffix: str) -> None:
    """性质断言（跨两档类名）：任何 BaseAppException 子类的 code 均非空且大写。"""
    cls = CostControlException if suffix == "Exception" else _SuffixlessFault
    exc = cls("probe")
    assert exc.code
    assert exc.code == exc.code.upper()
    assert exc.details == {}
    assert exc.cause is None
    assert isinstance(exc, Exception)
    assert isinstance(types.SimpleNamespace(), types.SimpleNamespace)
