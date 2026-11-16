# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""cost_control 缺口分支测试：BudgetManager 告警/预留释放/周期重置/单例 + 配置解析。

覆盖 coverage.xml 2026-09-13 口径缺行：
- record_usage 全局日/月硬上限（到达即中断，被拒用量不写入）；
- user_id 记账分支；
- release_reservation 预留回滚（task/session 整体释放、两作用域按较大值
  扣一次全局、无预留空释放幂等）；
- 跨月周期重置（换月/换年两个触发维度 + 本月不重置对照）；
- 告警 WARNING 级动作位与文案、同级别去重、回调（同步/异步/异常吞并/INFO 不回调）；
- get_budget_status 全局支与 exhausted/warning 分级、reset_session_budget、
  get_budget_manager 单例；
- CostControlConfig.get_model_cost_rate 三级匹配、get_user_budget 回落、
  load_cost_control_config manifest 注入解析（B2 收敛后唯一取数源，
  单一真值/两端点一致性细节见 test_cost_config_single_source.py）。

时间维度（跨月重置）沿用 tests/plugins/system/cost_control/test_budget_concurrency.py
的既有做法：直接改写 manager 的周期起点字段模拟时钟推进（时钟为外部依赖）。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

# 平铺裸名自举 + 逐出：exceptions/budget_manager/constants/config 等名字遍布各
# 插件（llm 有同名 exceptions.py），车道共跑时被别的插件占位会让下面的平铺
# import 打到错误实现（收集期 ImportError）。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
while _PLUGIN_DIR in sys.path:
    sys.path.remove(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_DIR)
for _bare in ("exceptions", "budget_manager", "constants", "config", "tokenizer"):
    sys.modules.pop(_bare, None)

from budget_manager import (
    BudgetAlert,
    BudgetAlertAction,
    BudgetAlertLevel,
    BudgetManager,
    get_budget_manager,
    reset_budget_manager,
)
from constants import CostControl
from exceptions import BudgetExceededException, QuotaExhaustedException

from config import (
    CostControlConfig,
    CostRates,
    GlobalBudget,
    ProtectionConfig,
    UserBudget,
    get_cost_control_config,
    load_cost_control_config,
)

pytestmark = pytest.mark.unit


def _make_manager(
    *,
    daily: int = 10_000,
    monthly: int = 10_000_000,
    per_task: int = 10_000_000,
    per_session: int = 10_000_000,
    alert_callback: Callable[[BudgetAlert], Any] | None = None,
    **protection: bool,
) -> BudgetManager:
    """小全局限额/放大单作用域限额的独立 BudgetManager（测试间零共享）。"""
    return BudgetManager(
        config=CostControlConfig(
            global_budget=GlobalBudget(
                daily_token_limit=daily,
                monthly_token_limit=monthly,
                per_task_token_limit=per_task,
                per_session_token_limit=per_session,
            ),
            protection=ProtectionConfig(**protection),
        ),
        alert_callback=alert_callback,
    )


def _month_start_of(now: datetime, *, month_offset: int = 0, year_offset: int = 0) -> datetime:
    """构造相对 now 偏移 month_offset 个月 / year_offset 年的月初时刻。"""
    total = now.month - 1 + month_offset
    month = total % 12 + 1
    year = now.year + year_offset + total // 12
    return now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


# ─────────────────────────────────────────────
# record_usage 全局日/月硬上限（到达即中断）
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_rejects_above_global_daily_limit() -> None:
    bm = _make_manager(daily=100)
    with pytest.raises(QuotaExhaustedException) as exc_info:
        await bm.record_usage(tokens=101, model="m")
    assert exc_info.value.quota_type == "daily"
    # 被拒用量不写入（到达即中断，不静默累加）
    stats = bm.get_usage_statistics()
    assert stats["global"]["daily_tokens"] == 0
    assert stats["global"]["monthly_tokens"] == 0


@pytest.mark.asyncio
async def test_record_usage_accepts_at_exact_global_daily_limit() -> None:
    """边界语义：恰好等于限额放行（超限判定为严格大于）。"""
    bm = _make_manager(daily=100)
    await bm.record_usage(tokens=100, model="m")
    assert bm.get_usage_statistics()["global"]["daily_tokens"] == 100
    assert bm.get_budget_status().used == 100


@pytest.mark.asyncio
async def test_record_usage_rejects_above_global_monthly_limit() -> None:
    """符号量级相反的组合：月限极小、日限极大 → 月面先拦。"""
    bm = _make_manager(daily=1_000_000, monthly=100)
    with pytest.raises(QuotaExhaustedException) as exc_info:
        await bm.record_usage(tokens=101, model="m")
    assert exc_info.value.quota_type == "monthly"
    assert bm.get_usage_statistics()["global"]["monthly_tokens"] == 0


# ─────────────────────────────────────────────
# user_id 记账分支
# ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [[("u1", 200), ("u2", 300)], [("alice", 50), ("bob", 950)]],
    ids=["two-users", "asymmetric-scale"],
)
async def test_record_usage_with_user_id_counts_into_global_ledgers(records: list[tuple[str, int]]) -> None:
    bm = _make_manager()
    total = 0
    for user_id, tokens in records:
        assert await bm.record_usage(tokens=tokens, model="m", user_id=user_id) is None
        total += tokens
        # 单调累计：全局日/月账本与逐次记账严格一致
        stats = bm.get_usage_statistics()
        assert stats["global"]["daily_tokens"] == total
        assert stats["global"]["monthly_tokens"] == total


# ─────────────────────────────────────────────
# release_reservation：预留兑付回滚
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_reservation_unblocks_task_budget_and_is_idempotent() -> None:
    bm = _make_manager(per_task=1000)
    await bm.check_budget(estimated_tokens=1000, task_id="t1")
    with pytest.raises(BudgetExceededException):
        await bm.check_budget(estimated_tokens=1, task_id="t1")

    await bm.release_reservation(task_id="t1")
    await bm.release_reservation(task_id="t1")  # 幂等：重复释放不抛
    await bm.check_budget(estimated_tokens=1000, task_id="t1")  # 预留已回滚，可再次通过


@pytest.mark.asyncio
async def test_release_reservation_both_scopes_deducts_global_once() -> None:
    """同一调用在 task/session 两作用域登记同数额：全局按较大值扣一次。"""
    bm = _make_manager(daily=1000)
    await bm.check_budget(estimated_tokens=500, task_id="t1", session_id="s1")
    await bm.check_budget(estimated_tokens=500, task_id="t2", session_id="s2")
    with pytest.raises(QuotaExhaustedException):
        await bm.check_budget(estimated_tokens=1)

    await bm.release_reservation(task_id="t1", session_id="s1")  # 释放 max(500, 500) = 500
    await bm.check_budget(estimated_tokens=400)  # 500 + 400 = 900 ≤ 1000
    with pytest.raises(QuotaExhaustedException):
        await bm.check_budget(estimated_tokens=200)  # 900 + 200 > 1000


@pytest.mark.asyncio
async def test_release_reservation_without_reservation_is_noop() -> None:
    """无预留时释放是空操作：全局额度不被误扣。"""
    bm = _make_manager(daily=1000)
    await bm.release_reservation(task_id="ghost", session_id="ghost")
    await bm.release_reservation(session_id="ghost")  # 仅 session 面
    await bm.release_reservation()  # 两面皆空
    await bm.check_budget(estimated_tokens=1000)  # 全部额度仍可用


# ─────────────────────────────────────────────
# 跨月周期重置
# ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("month_offset", "year_offset", "expected_monthly"),
    [
        (-1, 0, 50),  # 上个月 → 月账本重置
        (0, -1, 50),  # 去年同月（仅年份变化）→ 月账本重置
        (0, 0, 150),  # 本月 → 不重置（对照）
    ],
    ids=["prev-month", "prev-year", "same-month-control"],
)
async def test_monthly_period_resets_only_on_month_or_year_change(
    month_offset: int, year_offset: int, expected_monthly: int
) -> None:
    bm = _make_manager()
    await bm.record_usage(tokens=100, model="m")
    # 模拟时钟推进：把月起点改写为目标周期
    bm._month_start = _month_start_of(datetime.now(), month_offset=month_offset, year_offset=year_offset)

    await bm.record_usage(tokens=50, model="m")
    stats = bm.get_usage_statistics()
    assert stats["global"]["monthly_tokens"] == expected_monthly
    assert stats["global"]["daily_tokens"] == 150  # 日账本不受月重置影响


# ─────────────────────────────────────────────
# 告警分级 / 动作位 / 去重 / 回调
# ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tokens", "auto_save"),
    [
        (800, True),  # 边界：恰好 80% 即 WARNING
        (850, True),  # 区间中段
        (850, False),  # auto_save 关闭 → 无动作
    ],
    ids=["boundary-80%", "mid-85%", "auto-save-disabled"],
)
async def test_warning_alert_level_and_action(tokens: int, auto_save: bool) -> None:
    bm = _make_manager(daily=1000, auto_save_at_warning=auto_save)
    alert = await bm.record_usage(tokens=tokens, model="m")

    assert alert is not None
    assert alert.level == BudgetAlertLevel.WARNING
    assert alert.scope == "global"
    # 性质：落在 [80, 90) 区间
    assert 80.0 <= alert.usage_percent < 90.0
    assert alert.action_taken == (BudgetAlertAction.SAVE_CHECKPOINT if auto_save else None)
    # WARNING 文案为静态拷贝（不受 auto_save 开关影响）
    assert "检查点" in alert.message


@pytest.mark.asyncio
async def test_same_level_alert_deduplicated_and_refires_on_level_change() -> None:
    bm = _make_manager(daily=1000)
    first = await bm.record_usage(tokens=850, model="m")  # 85% → WARNING
    repeat = await bm.record_usage(tokens=10, model="m")  # 86% 仍 WARNING → 去重
    escalated = await bm.record_usage(tokens=100, model="m")  # 96% → CRITICAL 再触发

    assert first is not None
    assert first.level == BudgetAlertLevel.WARNING
    assert repeat is None
    assert escalated is not None
    assert escalated.level == BudgetAlertLevel.CRITICAL
    assert escalated.action_taken == BudgetAlertAction.PAUSE_EXECUTION


@pytest.mark.asyncio
async def test_sync_alert_callback_receives_alert() -> None:
    received: list[BudgetAlert] = []
    bm = _make_manager(daily=1000, alert_callback=received.append)
    alert = await bm.record_usage(tokens=850, model="m")

    assert alert is not None
    assert len(received) == 1
    assert received[0] is alert
    assert received[0].level == BudgetAlertLevel.WARNING


@pytest.mark.asyncio
async def test_async_alert_callback_receives_alert() -> None:
    received: list[BudgetAlert] = []

    async def callback(alert: BudgetAlert) -> None:
        received.append(alert)

    bm = _make_manager(daily=1000, alert_callback=callback)
    alert = await bm.record_usage(tokens=850, model="m")

    assert alert is not None
    assert len(received) == 1
    assert received[0] is alert


@pytest.mark.asyncio
async def test_alert_callback_failure_does_not_break_accounting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_callback(_alert: BudgetAlert) -> None:
        raise RuntimeError("callback down")

    bm = _make_manager(daily=1000, alert_callback=broken_callback)
    with caplog.at_level(logging.WARNING, logger="budget_manager"):
        alert = await bm.record_usage(tokens=850, model="m")

    # 回调异常被吞并：记账与告警返回不受影响
    assert alert is not None
    assert alert.level == BudgetAlertLevel.WARNING
    assert bm.get_usage_statistics()["global"]["daily_tokens"] == 850

    # 失败必须可见：warning 级日志携带原异常信息（成本安全面告警不得静默丢失）
    failure_records = [r for r in caplog.records if "告警回调执行失败" in r.message]
    assert len(failure_records) == 1
    assert failure_records[0].levelno == logging.WARNING
    assert "callback down" in failure_records[0].message


@pytest.mark.asyncio
async def test_info_level_skips_alert_and_callback() -> None:
    received: list[BudgetAlert] = []
    bm = _make_manager(daily=100_000, alert_callback=received.append)
    alert = await bm.record_usage(tokens=100, model="m")  # 0.1% → INFO

    assert alert is None
    assert received == []


# ─────────────────────────────────────────────
# get_budget_status 全局支与分级
# ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tokens", "expected_level"),
    [
        (1000, BudgetAlertLevel.EXHAUSTED),  # 100%
        (950, BudgetAlertLevel.CRITICAL),  # 95%
        (850, BudgetAlertLevel.WARNING),  # 85%
        (100, BudgetAlertLevel.INFO),  # 10%
    ],
    ids=["exhausted", "critical", "warning", "info"],
)
async def test_budget_status_global_scope_and_alert_levels(tokens: int, expected_level: BudgetAlertLevel) -> None:
    bm = _make_manager(daily=1000)
    await bm.record_usage(tokens=tokens, model="m")

    status = bm.get_budget_status()
    assert status.scope == "global"
    assert status.scope_id is None
    assert status.limit == 1000
    assert status.used == tokens
    assert status.remaining == 1000 - tokens
    assert status.alert_level == expected_level
    assert status.usage_percent == pytest.approx(tokens / 10)
    assert status.estimated_cost == pytest.approx(tokens / 1000 * 0.002)


# ─────────────────────────────────────────────
# reset_session_budget / get_budget_manager 单例
# ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("record_first", [True, False], ids=["recorded", "unknown-session"])
async def test_reset_session_budget_clears_session_ledger(record_first: bool) -> None:
    bm = _make_manager(per_session=1000)
    if record_first:
        await bm.record_usage(tokens=400, model="m", session_id="s1")
        assert bm.get_budget_status(session_id="s1").used == 400

    await bm.reset_session_budget("s1")
    assert bm.get_budget_status(session_id="s1").used == 0
    if record_first:
        # 会话账本清零不影响全局账本
        assert bm.get_usage_statistics()["global"]["daily_tokens"] == 400


def test_get_budget_manager_returns_singleton() -> None:
    reset_budget_manager()
    try:
        first = get_budget_manager()
        assert isinstance(first, BudgetManager)
        assert get_budget_manager() is first
    finally:
        reset_budget_manager()  # 防跨测试泄漏全局单例


# ─────────────────────────────────────────────
# CostControlConfig：费率匹配 / 用户预算回落
# ─────────────────────────────────────────────


class TestCostControlConfigLookup:
    def _config(self) -> CostControlConfig:
        return CostControlConfig(
            cost_rates=CostRates(default=0.002, models={"gpt-4": 0.03, "claude": 0.015})
        )

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gpt-4", 0.03),  # 精确命中
            ("gpt-4-turbo-2026", 0.03),  # 前缀命中
            ("claude-sonnet", 0.015),  # 另一前缀
            ("qwen-unknown", 0.002),  # 无命中 → 默认费率
        ],
    )
    def test_get_model_cost_rate_exact_prefix_default(self, model: str, expected: float) -> None:
        rate = self._config().get_model_cost_rate(model)
        assert rate == pytest.approx(expected)
        assert rate > 0

    @pytest.mark.parametrize("level", ["vip", "guest"], ids=["registered", "fallback"])
    def test_get_user_budget_registered_and_fallback(self, level: str) -> None:
        custom = UserBudget(daily_token_limit=5_000_000, monthly_token_limit=50_000_000)
        cfg = CostControlConfig(user_budgets={"vip": custom})
        budget = cfg.get_user_budget(level)
        if level == "vip":
            assert budget == custom
        else:
            assert budget == UserBudget()  # 未登记等级回落默认


# ─────────────────────────────────────────────
# load_cost_control_config：manifest 注入解析
# （B2 收敛：执法面唯一取数源 = 内核 config_files[id=cost] 注入，
# 无 yaml/config_center 读取路径；单一真值细节见 test_cost_config_single_source.py）
# ─────────────────────────────────────────────


class TestLoadCostControlConfig:
    def test_parses_full_injected_payload(self) -> None:
        cfg = load_cost_control_config(
            {
                "enabled": True,
                "global_config": {
                    "daily_token_limit": 8000,
                    "monthly_token_limit": 9_000_000,
                    "per_task_token_limit": 4000,
                    "per_session_token_limit": 6000,
                },
                "alerts": {"warning_threshold": 50, "critical_threshold": 70, "exhausted_threshold": 95},
                "protection": {
                    "auto_save_at_warning": False,
                    "auto_pause_at_critical": False,
                    "auto_stop_at_exhausted": False,
                },
            }
        )

        assert cfg.global_budget == GlobalBudget(
            daily_token_limit=8000,
            monthly_token_limit=9_000_000,
            per_task_token_limit=4000,
            per_session_token_limit=6000,
        )
        # 设置页百分刻度 → 执法面小数刻度
        assert cfg.alerts.warning_threshold == pytest.approx(0.5)
        assert cfg.alerts.critical_threshold == pytest.approx(0.7)
        assert cfg.alerts.exhausted_threshold == pytest.approx(0.95)
        assert cfg.protection.auto_save_at_warning is False
        assert cfg.protection.auto_pause_at_critical is False
        assert cfg.protection.auto_stop_at_exhausted is False
        # 注入形态无费率/用户预算段 → 默认
        assert cfg.cost_rates.default == pytest.approx(0.002)
        assert cfg.user_budgets == {}
        # 解析结果落位单例
        assert get_cost_control_config() is cfg

    def test_partial_payload_keeps_defaults_for_absent_sections(self) -> None:
        cfg = load_cost_control_config({"alerts": {"warning_threshold": 30}})

        assert cfg.alerts.warning_threshold == pytest.approx(0.3)
        # 缺省段回落内置默认（constants 与设置页同值）
        assert cfg.alerts.critical_threshold == pytest.approx(
            CostControl.CRITICAL_THRESHOLD_PERCENT / 100
        )
        assert cfg.global_budget.daily_token_limit == CostControl.DAILY_TOKEN_LIMIT

    @pytest.mark.parametrize(
        "injected", [None, {}, "not-a-dict"], ids=["none", "empty-dict", "non-dict"]
    )
    def test_invalid_injection_falls_back_to_defaults(self, injected: Any) -> None:
        cfg = load_cost_control_config(injected)
        assert cfg == CostControlConfig()
