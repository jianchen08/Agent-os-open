# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""BudgetManager 预算竞态（F-CC-1）测试——check→record 原子预留语义。

意图（WHY）：
- 并发 LLM 调用（decorators.py @budget_check：check→call→record）若 check 与 record
  是两次独立加锁，会全部先过 check 再各自 record，per-task/per-session 预算被真实突破。
  本测试用屏障保证「所有 check 先完成、再统一 record」，钉死不变量：
  无论多少并发调用，总用量不得超过预算。
- record_usage 必须强制上限：超额时拒绝（raise），而非静默累加——否则漏网的
  调用会永久突破预算。
- 正常路径：check→record 正常累计、reset 后清零（含预留释放），不被破坏。
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# 预算与调用规模：8×300=2400 > task/session 限制 1000，并发必然触及上限
TASK_LIMIT = 1000
SESSION_LIMIT = 1000
DAILY_LIMIT = 10000
MONTHLY_LIMIT = 100000
N_WORKERS = 8
PER_CALL = 300
MAX_ALLOWED = TASK_LIMIT // PER_CALL  # 3


def _make_manager():
    """小预算配置的 BudgetManager（独立实例，测试间零共享）。"""
    from budget_manager import BudgetManager

    from config import CostControlConfig, GlobalBudget

    config = CostControlConfig(
        global_budget=GlobalBudget(
            daily_token_limit=DAILY_LIMIT,
            monthly_token_limit=MONTHLY_LIMIT,
            per_task_token_limit=TASK_LIMIT,
            per_session_token_limit=SESSION_LIMIT,
        )
    )
    return BudgetManager(config=config)


# ─────────────────────────────────────────────
# 并发突破（8 并发：最多 3 个放行，总用量 ≤ 1000）
# ─────────────────────────────────────────────

async def _run_concurrent_workers(bm, *, scope: str, scope_id: str) -> tuple[int, int]:
    """并发 check→record；屏障保证全部 check 先完成、再统一 record。

    前 3 个 check 通过（预留累积 ≤ 上限），其余因预留超限被拒，在屏障
    超时（1s）后照常 record——最终用量仍 ≤ 上限。
    """
    go = asyncio.Event()
    checked = 0
    succeeded = 0

    async def worker() -> None:
        nonlocal checked, succeeded
        await bm.check_budget(estimated_tokens=PER_CALL, **{scope: scope_id})
        checked += 1
        if checked == N_WORKERS:
            go.set()
        try:
            await asyncio.wait_for(go.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass  # 部分 check 被拒：不再等全部，模拟真实调用继续执行
        await bm.record_usage(tokens=PER_CALL, model="test-model", **{scope: scope_id})
        succeeded += 1

    await asyncio.gather(*(worker() for _ in range(N_WORKERS)), return_exceptions=True)
    return checked, succeeded


@pytest.mark.asyncio
async def test_concurrent_task_workers_never_exceed_task_budget() -> None:
    """并发任务调用：总用量不得超过 per_task 预算（check→record TOCTOU 场景）。

    8 个并发调用中最多 3 个放行、used ≤ 1000。
    """
    bm = _make_manager()
    checked, succeeded = await _run_concurrent_workers(bm, scope="task_id", scope_id="task_cc_1")

    status = bm.get_budget_status(task_id="task_cc_1")
    assert status.used <= TASK_LIMIT, f"并发下总用量突破预算: used={status.used} > {TASK_LIMIT}"
    assert checked <= MAX_ALLOWED, (
        f"并发下 {checked} 个 check 全部放行（TOCTOU 复现），最多应放行 {MAX_ALLOWED} 个"
    )
    assert succeeded <= MAX_ALLOWED, f"并发下 {succeeded} 个调用全部完成记账"


@pytest.mark.asyncio
async def test_concurrent_session_workers_never_exceed_session_budget() -> None:
    """并发会话调用：总用量不得超过 per_session 预算（P1-1 的 session 面）。"""
    bm = _make_manager()
    checked, succeeded = await _run_concurrent_workers(bm, scope="session_id", scope_id="sess_cc_1")

    status = bm.get_budget_status(session_id="sess_cc_1")
    assert status.used <= SESSION_LIMIT, f"并发下会话用量突破预算: used={status.used}"
    assert checked <= MAX_ALLOWED, f"并发下 {checked} 个 check 全部放行（TOCTOU 复现）"
    assert succeeded <= MAX_ALLOWED


# ─────────────────────────────────────────────
# 超额拦截（record 超限必须 raise，不静默累加）
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_usage_rejects_usage_above_task_limit() -> None:
    """record_usage 超过任务上限必须拒绝（raise），而非静默累加。

    审计 P1-2：第二次 record 触发 BudgetExceededException 且被拒用量不写入。
    """
    from exceptions import BudgetExceededException

    bm = _make_manager()
    await bm.check_budget(estimated_tokens=600, task_id="task_cc_2")
    await bm.record_usage(tokens=600, model="m", task_id="task_cc_2")

    with pytest.raises(BudgetExceededException):
        await bm.record_usage(tokens=500, model="m", task_id="task_cc_2")

    status = bm.get_budget_status(task_id="task_cc_2")
    assert status.used == 600, f"被拒的用量不应写入: used={status.used}"


@pytest.mark.asyncio
async def test_record_usage_rejects_usage_above_session_limit() -> None:
    """record_usage 超过会话上限必须拒绝（P1-2 的 session 面）。"""
    from exceptions import BudgetExceededException

    bm = _make_manager()
    await bm.check_budget(estimated_tokens=700, session_id="sess_cc_2")
    await bm.record_usage(tokens=700, model="m", session_id="sess_cc_2")

    with pytest.raises(BudgetExceededException):
        await bm.record_usage(tokens=400, model="m", session_id="sess_cc_2")

    assert bm.get_budget_status(session_id="sess_cc_2").used == 700


@pytest.mark.asyncio
async def test_record_usage_without_check_still_enforces_limit() -> None:
    """无 check 直接 record 超限同样必须拒绝（硬上限与预留路径无关）。"""
    from exceptions import BudgetExceededException

    bm = _make_manager()
    with pytest.raises(BudgetExceededException):
        await bm.record_usage(tokens=1500, model="m", task_id="task_cc_3")
    assert bm.get_budget_status(task_id="task_cc_3").used == 0


# ─────────────────────────────────────────────
# 正常路径（check→record 累计 / reset 清零）
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_record_normal_accumulate_and_reset() -> None:
    """正常路径：check→record 正确累计；reset 清零用量且释放预留。"""
    from exceptions import BudgetExceededException

    bm = _make_manager()
    await bm.check_budget(estimated_tokens=300, task_id="task_cc_4")
    await bm.record_usage(tokens=300, model="m", task_id="task_cc_4")
    await bm.check_budget(estimated_tokens=300, task_id="task_cc_4")
    await bm.record_usage(tokens=300, model="m", task_id="task_cc_4")
    assert bm.get_budget_status(task_id="task_cc_4").used == 600

    await bm.reset_task_budget("task_cc_4")
    assert bm.get_budget_status(task_id="task_cc_4").used == 0

    # reset 后预留一并释放：300×3=900 ≤ 1000 可连续通过，第 4 个被拒
    for _ in range(3):
        await bm.check_budget(estimated_tokens=300, task_id="task_cc_4")
    with pytest.raises(BudgetExceededException):
        await bm.check_budget(estimated_tokens=300, task_id="task_cc_4")


# ─────────────────────────────────────────────
# decorators.py @budget_check 路径
# ─────────────────────────────────────────────

def _patch_decorator_deps(bm) -> ExitStack:
    """把装饰器内部的单例与 token 计数换成测试替身（返回 ExitStack 上下文）。"""
    stack = ExitStack()
    stack.enter_context(patch("decorators.get_budget_manager", return_value=bm))
    stack.enter_context(patch("decorators.get_token_counter"))
    return stack


@pytest.mark.asyncio
async def test_decorator_blocks_call_when_budget_exceeded() -> None:
    """@budget_check 超限拦截语义保持：check 不过则 LLM 函数不执行。"""
    from decorators import budget_check
    from exceptions import BudgetExceededException

    bm = _make_manager()
    called = False

    with _patch_decorator_deps(bm):
        @budget_check(estimated_tokens=1500, task_id_param="task_id")
        async def fake_llm(task_id: str) -> str:
            nonlocal called
            called = True
            return "ok"

        with pytest.raises(BudgetExceededException):
            await fake_llm(task_id="task_cc_5")

    assert called is False, "预算超限时 LLM 函数不应被执行"


@pytest.mark.asyncio
async def test_decorator_concurrent_calls_never_exceed_task_budget() -> None:
    """装饰器完整路径（check→call→record）并发下同样不得突破预算。"""
    from decorators import budget_check

    bm = _make_manager()
    entered = 0
    go = asyncio.Event()

    with _patch_decorator_deps(bm):
        @budget_check(estimated_tokens=PER_CALL, task_id_param="task_id")
        async def fake_llm(task_id: str) -> str:
            nonlocal entered
            entered += 1
            if entered == N_WORKERS:
                go.set()
            try:
                await asyncio.wait_for(go.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            return "ok"

        results = await asyncio.gather(
            *(fake_llm(task_id="task_cc_6") for _ in range(N_WORKERS)),
            return_exceptions=True,
        )

    ok = [r for r in results if r == "ok"]
    status = bm.get_budget_status(task_id="task_cc_6")
    assert status.used <= TASK_LIMIT, f"装饰器并发路径突破预算: used={status.used}"
    assert len(ok) <= MAX_ALLOWED, (
        f"装饰器并发路径 {len(ok)} 个调用全部放行（TOCTOU 复现），最多应 {MAX_ALLOWED} 个"
    )


@pytest.mark.asyncio
async def test_decorator_releases_reservation_on_call_failure() -> None:
    """LLM 调用失败时释放预留，避免预算被幽灵占用（回归守护）。"""
    from decorators import budget_check

    bm = _make_manager()

    with _patch_decorator_deps(bm):
        @budget_check(estimated_tokens=300, task_id_param="task_id")
        async def failing_llm(task_id: str) -> str:
            raise RuntimeError("llm call failed")

        with pytest.raises(RuntimeError):
            await failing_llm(task_id="task_cc_7")

    # 失败释放后：预留不残留，300×3=900 ≤ 1000 仍可连续通过
    for _ in range(3):
        await bm.check_budget(estimated_tokens=300, task_id="task_cc_7")


# ─────────────────────────────────────────────
# 全局预算事前拦截（F-COST-2：global 日/月 预留 + 到达即中断）
# 产品决定：全局日/月限额同样事前预留拦截，check 阶段即拒，
# 而非 F-CC-1 现状的「check 全过、record 才拒（token 已花）」。
# ─────────────────────────────────────────────

# 全局限额测试规模：8×300=2400 > 全局日限 1000，并发必然触及上限
GLOBAL_DAILY_LIMIT = 1000
GLOBAL_MONTHLY_LIMIT_TIGHT = 1000
G_WORKERS = 8
G_PER_CALL = 300
G_MAX_ALLOWED = GLOBAL_DAILY_LIMIT // G_PER_CALL  # 3


def _make_global_manager(*, daily_limit: int = GLOBAL_DAILY_LIMIT, monthly_limit: int = 100_000):
    """小全局限额的 BudgetManager（task/session 限额放大，隔离全局面）。

    全局测试不传 task_id/session_id，故 per_task/per_session 不生效；仅全局限额绑定。
    """
    from budget_manager import BudgetManager

    from config import CostControlConfig, GlobalBudget

    config = CostControlConfig(
        global_budget=GlobalBudget(
            daily_token_limit=daily_limit,
            monthly_token_limit=monthly_limit,
            per_task_token_limit=10_000_000,
            per_session_token_limit=10_000_000,
        )
    )
    return BudgetManager(config=config)


async def _run_concurrent_global_workers(
    bm, *, n: int = G_WORKERS, per_call: int = G_PER_CALL
) -> tuple[int, int]:
    """并发 check→record（无 task/session，仅全局限额生效）；屏障保证全部 check 先完成。

    前 3 个 check 通过（reserved 累积计入占用），第 4 个因全局预留超限
    被拒，从源头中断使用（而非等 record 阶段才拒）。
    """
    go = asyncio.Event()
    checked = 0
    succeeded = 0

    async def worker() -> None:
        nonlocal checked, succeeded
        await bm.check_budget(estimated_tokens=per_call)
        checked += 1
        if checked == n:
            go.set()
        try:
            await asyncio.wait_for(go.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass  # 部分 check 被拒：不再等全部，模拟真实调用继续执行
        await bm.record_usage(tokens=per_call, model="test-model")
        succeeded += 1

    await asyncio.gather(*(worker() for _ in range(n)), return_exceptions=True)
    return checked, succeeded


@pytest.mark.asyncio
async def test_concurrent_global_workers_never_exceed_daily_budget() -> None:
    """并发全局调用：总用量不得超过全局日限，且超限在 check 阶段即拒。

    F-COST-2 核心断言：8 个并发 check 中最多 3 个放行、第 4 个即拒
    （全局 reserved 计入占用），超限在 check 阶段拦截。
    """
    from exceptions import QuotaExhaustedException

    bm = _make_global_manager(monthly_limit=100_000)
    checked, succeeded = await _run_concurrent_global_workers(bm)

    stats = bm.get_usage_statistics()
    daily_tokens = stats["global"]["daily_tokens"]
    assert daily_tokens <= GLOBAL_DAILY_LIMIT, (
        f"并发下全局日用量突破预算: used={daily_tokens} > {GLOBAL_DAILY_LIMIT}"
    )
    assert checked <= G_MAX_ALLOWED, (
        f"并发下 {checked} 个全局 check 全部放行（TOCTOU 复现），最多应放行 {G_MAX_ALLOWED} 个"
    )
    assert succeeded <= G_MAX_ALLOWED, f"并发下 {succeeded} 个调用全部完成记账"
    # 到达即中断：被拒的 check 抛 QuotaExhaustedException（而非静默返回 False）
    assert checked < G_WORKERS


@pytest.mark.asyncio
async def test_check_rejected_when_global_daily_exhausted() -> None:
    """全局日限到达即中断：usage 达上限后，下一次 check 直接拒绝（非 record 才拒）。

    意图：全局限额是「使用即中断」的硬边界——到达预算后续调用在 check 阶段
    就被拒，避免 token 已花才补救。
    """
    from exceptions import QuotaExhaustedException

    bm = _make_global_manager(monthly_limit=100_000)
    await bm.check_budget(estimated_tokens=GLOBAL_DAILY_LIMIT)
    await bm.record_usage(tokens=GLOBAL_DAILY_LIMIT, model="m")

    with pytest.raises(QuotaExhaustedException):
        await bm.check_budget(estimated_tokens=1)


@pytest.mark.asyncio
async def test_check_rejected_when_global_monthly_exhausted() -> None:
    """全局月限到达即中断（月限面的对称守护，与日限同机制）。"""
    from exceptions import QuotaExhaustedException

    bm = _make_global_manager(daily_limit=100_000, monthly_limit=GLOBAL_MONTHLY_LIMIT_TIGHT)
    await bm.check_budget(estimated_tokens=GLOBAL_MONTHLY_LIMIT_TIGHT)
    await bm.record_usage(tokens=GLOBAL_MONTHLY_LIMIT_TIGHT, model="m")

    with pytest.raises(QuotaExhaustedException):
        await bm.check_budget(estimated_tokens=1)


@pytest.mark.asyncio
async def test_global_reservation_cleared_on_daily_reset() -> None:
    """周期重置同步清全局预留：累积预留被清零后可继续 check。

    意图：check 通过即预留（不 record 模拟在途/失败调用），多次 check 累积预留
    直至超限被拒（此时 usage 仍为 0——纯预留驱动）；跨天重置后
    _global_daily_reserved 一并清零，新 check 不被幽灵预留拖累。
    """
    from exceptions import QuotaExhaustedException

    bm = _make_global_manager(monthly_limit=100_000)
    # 仅 check 不 record：累积全局日预留 3×300=900（usage 仍为 0）
    for _ in range(G_MAX_ALLOWED):
        await bm.check_budget(estimated_tokens=G_PER_CALL)
    # 第 4 个 check 因预留累积（900+300=1200>1000）被拒——usage 仍为 0
    with pytest.raises(QuotaExhaustedException):
        await bm.check_budget(estimated_tokens=G_PER_CALL)
    assert bm.get_usage_statistics()["global"]["daily_tokens"] == 0

    # 触发跨天重置（控制时间），_global_daily_reserved 同步清零
    bm._day_start = datetime.now() - timedelta(days=2)
    # 重置后全局预留清零：新 check 不再被幽灵预留阻塞
    await bm.check_budget(estimated_tokens=G_PER_CALL)  # 不应抛
