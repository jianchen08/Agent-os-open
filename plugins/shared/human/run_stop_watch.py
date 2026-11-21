"""run 停止看门狗：阻塞等待期感知 run 停止信号，让工具立刻收口返回。

## 为什么需要（与 `plugins/shared/bash/run_abort_watch.py` 同一根因，同为插件侧落地）

引擎的待输入队列消费点只有一处：每次 **LLM 迭代开轮前**
（`kernel/crates/engine/src/pipeline_loop.rs:814` → `:1101 inject_pending_inputs`），
`tool_execute` 迭代不消费。`human_interaction` 的阻塞等待缺省 86400s（24h），
run 卡在一次等待里时注入消息只能排队，run 永远走不到下一个 LLM 迭代——
外部表现「注入了没反应 / 永久排队」。

停止通道写面（`dispatch_stop` / `suspend_for_force` → `ws_session.rs` 的
`suspend_pipeline_execution`）落 `run_status=suspended` 并唤醒内核停止信号
中枢；能消费该信号的只有：llm 插件（`wait_run_stop` 长轮询，中断流式）、
bash 工具（旁路轮询，整树杀前台子进程）。**human 工具此前无人监听**，
所以它必须把 24h 等待切成可取消的窗口：命中 `suspended` 即取消交互请求
并返回 `RUN_STOPPED`，run 立刻回到 step 边界按既有停止语义收尾，派发链
随后 drain 队列里排队的注入消息起跑新一轮。

## 读面与写面同源

状态来源 = 内核既有只读能力 `service-registry.pipeline-runs.list_by_pipeline`
（兜底 `pipeline-state.list`），数据源是 `pipeline_state` 表的 run 簿记键
（`kernel/crates/engine/src/store.rs::load_run_projection`）——与
`dispatch_stop`/`suspend_for_force` 写 `run_status=suspended` 的
`upsert_state_fields` 是同一张表同一键，不存在第二真值。

## 误杀防护（不按耗时猜卡死）

- 只在 `run_status` **显式等于 `suspended`** 时收口：正常等待用户作答的
  交互（几分钟甚至几小时）不会被判超时、不会被杀；
- 轮询间隔可配（`AGENTOS_RUN_ABORT_POLL_SECS`，与 bash 同一把旋钮，缺省
  2s，`<=0`/非法值 = 完全关闭）；
- 读面异常按「不可判定」处理——只记 debug，交互照常等待（通道故障不等于
  run 被停止）；
- 能力句柄缺席（老内核/未注入）= 看门狗关闭，工具本体行为与旧版一致。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# 轮询间隔环境变量（秒）：与 bash 看门狗共用一把旋钮（同一台机器一个意图）。
ENV_POLL_SECS = "AGENTOS_RUN_ABORT_POLL_SECS"
DEFAULT_POLL_SECS = 2.0

# 状态读能力：主路径按管道读 run 簿记投影，兜底读 state 聚合行的 run_status。
PRIMARY_CAPABILITY = "service-registry"
PRIMARY_METHOD = "pipeline-runs.list_by_pipeline"
FALLBACK_CAPABILITY = "pipeline-state"
FALLBACK_METHOD = "list"

# 单次读状态调用超时（秒）：读面卡住不许拖住看门狗自身。
READ_TIMEOUT_SECS = 5.0

# 停止判定词：内核 `RunStatus::Suspended` 的序列化值（running/completed/
# failed/cancelled 都不构成收口理由）。
STOP_STATUS = "suspended"

# 状态读器签名：无参协程 → 当前 run 状态串（None/未知 = 不可判定）。
StatusReader = Callable[[], Awaitable[str | None]]


def resolve_poll_secs(environ: Mapping[str, str] | None = None) -> float:
    """解析轮询间隔（秒）。`<=0` 或非法值按「关闭」处理（返回 0）。"""
    raw = (environ if environ is not None else os.environ).get(ENV_POLL_SECS)
    if raw is None or not str(raw).strip():
        return DEFAULT_POLL_SECS
    try:
        secs = float(str(raw).strip())
    except ValueError:
        logger.warning("[human_interaction] %s=%r 非法，停止看门狗关闭", ENV_POLL_SECS, raw)
        return 0.0
    if secs <= 0:
        return 0.0
    return secs


def status_of_row(row: Any) -> str:
    """从一行 run/state 记录取状态串（`status` 优先，兜底 `run_status`）。"""
    if not isinstance(row, dict):
        return ""
    for key in ("status", "run_status"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def pick_row_status(rows: Any, pipeline_id: str) -> str | None:
    """从读面返回的行集合里取**本管道**的状态。

    `pipeline-state.list` 全租户返回多管道行：必须按 `pipeline_id` 精确匹配，
    不得拿别的管道的 `suspended` 当本 run 的停止理由（否则会误取消当前交互）。
    无匹配行 → `None`（不可判定）。
    """
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_pid = row.get("pipeline_id")
        if isinstance(row_pid, str) and row_pid and row_pid != pipeline_id:
            continue
        status = status_of_row(row)
        if status:
            return status
    return None


class RunStopWatcher:
    """后台轮询 run 状态，命中停止条件即完成 [`wait`]。

    被测对象只依赖注入的 `read_status` 协程 + 轮询间隔，不碰网络——
    判定逻辑可用假读器直接验证。
    """

    def __init__(
        self,
        read_status: StatusReader,
        poll_secs: float = DEFAULT_POLL_SECS,
        pipeline_id: str = "",
    ) -> None:
        self._read_status = read_status
        self.poll_secs = poll_secs
        self.pipeline_id = pipeline_id
        self.stopped = False
        self.polls = 0

    def stop(self) -> None:
        """置停止位（幂等）：下一次循环检查即退出。"""
        self.stopped = True

    async def wait(self) -> bool:
        """轮询直到检出停止信号（True）或被 [`stop`] 叫停（False）。

        读面异常按不可判定处理（记 debug 后继续下一窗）——通道故障不等于
        run 被停止，不得据此打断用户交互。
        """
        while not self.stopped:
            await asyncio.sleep(self.poll_secs)
            if self.stopped:
                return False
            self.polls += 1
            try:
                status = await self._read_status()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 通道故障降级为不可判定
                logger.debug(
                    "[human_interaction] run 状态读失败（本轮不可判定，继续等待）pipeline=%s",
                    self.pipeline_id,
                    exc_info=True,
                )
                continue
            if status == STOP_STATUS:
                logger.warning(
                    "[human_interaction] 检出 run 停止信号（run_status=suspended），"
                    "取消在等交互并让出 run pipe=%s polls=%d",
                    self.pipeline_id,
                    self.polls,
                )
                return True
        return False


def build_status_reader(plugin: Any, pipeline_id: str) -> StatusReader | None:
    """从插件能力句柄构建状态读器；能力全缺席 → None（看门狗关闭）。"""
    for capability_name, method in (
        (PRIMARY_CAPABILITY, PRIMARY_METHOD),
        (FALLBACK_CAPABILITY, FALLBACK_METHOD),
    ):
        try:
            handle = plugin.get_capability(capability_name)
        except Exception:  # noqa: BLE001 —— 能力未注入 = 看门狗关闭
            continue
        if handle is None:
            continue

        async def _read(handle: Any = handle, method: str = method) -> str | None:
            rows = await handle.call(method, {"pipeline_id": pipeline_id}, READ_TIMEOUT_SECS)
            return pick_row_status(rows, pipeline_id)

        return _read
    return None


def start_watcher(plugin: Any, pipeline_id: str) -> RunStopWatcher | None:
    """装配看门狗（不启动）。返回 None = 本次调用不启用。

    启用条件全部满足才建：pipeline_id 非空 + 轮询间隔 > 0 + 状态读能力可用。
    """
    pipeline_id = str(pipeline_id or "").strip()
    if not pipeline_id:
        return None
    poll_secs = resolve_poll_secs()
    if poll_secs <= 0:
        return None
    reader = build_status_reader(plugin, pipeline_id)
    if reader is None:
        logger.debug(
            "[human_interaction] 无 run 状态读能力（%s/%s），停止看门狗关闭",
            PRIMARY_CAPABILITY,
            FALLBACK_CAPABILITY,
        )
        return None
    return RunStopWatcher(reader, poll_secs, pipeline_id)


async def race_with_stop(
    work: Awaitable[Any],
    watcher: RunStopWatcher | None,
) -> tuple[bool, Any]:
    """跑 `work`，与停止看门狗赛跑；停止先到 → 取消 `work` 并返回 `(True, None)`。

    看门狗缺席 → 直通（`(False, 工作结果)`），行为与旧版一致。`work` 被取消后
    的收尾由调用方负责（例如 `service.cancel_request` 让前端交互卡消失）。
    """
    work_task: asyncio.Future[Any] = asyncio.ensure_future(work)
    if watcher is None:
        return False, await work_task

    stop_task: asyncio.Future[Any] = asyncio.ensure_future(watcher.wait())
    try:
        done, _pending = await asyncio.wait(
            {work_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        watcher.stop()
        _cancel(work_task)
        _cancel(stop_task)
        raise

    if stop_task in done and not stop_task.cancelled() and stop_task.exception() is None:
        if stop_task.result():
            _cancel(work_task)
            with _suppress_cancel():
                await work_task
            return True, None

    _cancel(stop_task)
    with _suppress_cancel():
        await stop_task
    return False, await work_task


class _suppress_cancel:
    """吞掉取消异常（收尾 await 已取消任务时用；其他异常照常上抛）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)


def _cancel(task: asyncio.Future[Any]) -> None:
    """取消未完成任务（已完成/已取消则幂等空转）。"""
    if not task.done():
        task.cancel()
