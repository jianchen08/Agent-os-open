"""run 中止看门狗：长工具调用期间感知 run 停止信号，触发整树杀前台子进程。

## 为什么需要

引擎的注入队列消费点只有一处：每次 **LLM 迭代开轮前**
（`kernel/crates/engine/src/pipeline_loop.rs:814` → `:1101 inject_pending_inputs`），
`tool_execute` 迭代不消费。run 卡在一次长工具调用里时：

- 用户点停止 → `dispatch_stop` 落 `run_status=suspended` + 信号；
  llm 插件经 `pipeline-executor.wait_run_stop` 长轮询即时中断流式；
- 但 **工具插件没有任何停止监听**，引擎侧的 `suspended` 只在 step/轮边界
  被检查——于是要等这次工具调用自己返回，最坏等于工具界（bash 声明
  `timeout_ms: 660000`，即 11 分钟）；
- 期间用户/调度方注入的消息只能排队，run 走不到下一个 LLM 迭代。

本模块在工具执行期间旁路轮询当前 run 状态，见到 `suspended` 即置中止事件，
由工具把前台子进程整树杀掉并快速返回——run 立刻回到 step 边界，引擎按既有
`should_stop`/`suspended` 语义收尾，派发链随后 drain 队列里的注入消息。

## 读面与写面同源

状态来源 = 内核既有只读能力 `service-registry.pipeline-runs.list_by_pipeline`
（兜底 `pipeline-state.list`），数据源是 `pipeline_state` 表的 run 簿记键
（`kernel/crates/engine/src/store.rs::load_run_projection`）——与
`dispatch_stop`/`suspend_for_force` 写 `run_status=suspended` 的
`upsert_state_fields` 是同一张表同一键，不存在第二真值。

## 误杀防护（不按耗时猜卡死）

- 只在 `run_status` **显式等于 `suspended`** 时中止：正常长任务（编译/下载/
  长测试）不会被判超时，也不会因跑到某个秒数被杀；
- 轮询间隔可配（`AGENTOS_RUN_ABORT_POLL_SECS`，缺省 2s，`<=0` 完全关闭）；
- 能力通道缺失/报错按「不可判定」处理——只记 debug 日志，工具照常执行，
  绝不因看门狗故障阻断正常命令。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# 轮询间隔环境变量（秒）：缺省 2s（停止感知延迟 ≈ 2s + 前台轮询 0.5s）；
# 0 与负值 = 关闭看门狗（回到旧行为）。
ENV_POLL_SECS = "AGENTOS_RUN_ABORT_POLL_SECS"
DEFAULT_POLL_SECS = 2.0

# 状态读能力：主路径按管道读 run 簿记投影，兜底读 state 聚合行的 run_status。
PRIMARY_CAPABILITY = "service-registry"
PRIMARY_METHOD = "pipeline-runs.list_by_pipeline"
FALLBACK_CAPABILITY = "pipeline-state"
FALLBACK_METHOD = "list"

# 单次读状态调用超时（秒）：读面卡住不许拖住看门狗自身。
READ_TIMEOUT_SECS = 5.0

# 中止判定词：内核 `RunStatus::Suspended` 的序列化值（其余 running/completed/
# failed/cancelled 都不构成中止理由）。
ABORT_STATUS = "suspended"

# 状态读器签名：无参协程 → 当前 run 状态串（None/未知 = 不可判定）。
StatusReader = Callable[[], Awaitable[str | None]]


def resolve_poll_secs(environ: Mapping[str, str] | None = None) -> float:
    """解析轮询间隔（秒）。`<=0` 或非法值按「关闭」处理（返回 0）。

    非法值（非数字）不得静默变成默认值——它意味着配置意图不明，按关闭
    处理更安全（看门狗是增强能力，缺省行为必须与旧版一致）。
    """
    raw = (environ if environ is not None else os.environ).get(ENV_POLL_SECS)
    if raw is None or not str(raw).strip():
        return DEFAULT_POLL_SECS
    try:
        secs = float(str(raw).strip())
    except ValueError:
        logger.warning("[bash_tool] %s=%r 非法，看门狗关闭", ENV_POLL_SECS, raw)
        return 0.0
    if secs <= 0:
        return 0.0
    return secs


def status_of_row(row: Any) -> str:
    """从一行 run/state 记录取状态串（`status` 优先，兜底 `run_status`）。

    值可能是串，也可能是内核枚举的序列化对象（`{"Suspended": ...}` 形态容错
    不应出现，但读面跨版本时按不判定处理——返回空串）。
    """
    if not isinstance(row, dict):
        return ""
    for key in ("status", "run_status"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def pick_row_status(rows: Any, pipeline_id: str) -> str | None:
    """从读面返回的行集合里取**本管道**的状态。

    行集合可能含多个管道（`pipeline-state.list` 全租户返回）：必须按
    `pipeline_id` 精确匹配，不得拿别的管道的 suspended 当本 run 的中止理由
    （否则会误杀当前工具调用）。无匹配行 → `None`（不可判定）。
    """
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_pid = row.get("pipeline_id")
        if isinstance(row_pid, str) and row_pid and row_pid != pipeline_id:
            continue
        # 行集合来自 list_by_pipeline 时可能省略 pipeline_id（已按管道过滤）
        status = status_of_row(row)
        if status:
            return status
    return None


class RunAbortWatcher:
    """后台轮询 run 状态，命中中止条件即置 [`event`]。

    被测对象只依赖注入的 `read_status` 协程 + 轮询间隔，不碰进程/网络——
    判定逻辑可用假时钟与假读器直接验证。
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
        self.event = asyncio.Event()
        # 停止位：工具调用结束（无论正常/异常/中止）由持有方置位，
        # 让轮询协程立即退出，不留孤儿轮询。
        self.stopped = False
        self.polls = 0

    def stop(self) -> None:
        """置停止位（幂等）：下一次循环检查即退出。"""
        self.stopped = True

    async def watch(self) -> None:
        """轮询主循环：状态 = suspended → 置中止事件并返回。

        读面异常按不可判定处理（记 debug 后继续下一窗）——通道故障不等于
        run 被停止，不得据此中止工具。
        """
        while not self.stopped:
            await asyncio.sleep(self.poll_secs)
            if self.stopped:
                return
            self.polls += 1
            try:
                status = await self._read_status()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 通道故障降级为不可判定
                logger.debug(
                    "[bash_tool] run 状态读失败（本轮不可判定，继续）pipeline=%s",
                    self.pipeline_id,
                    exc_info=True,
                )
                continue
            if status == ABORT_STATUS:
                logger.warning(
                    "[bash_tool] 检出 run 停止信号（run_status=suspended），中止在飞前台命令 pipe=%s polls=%d",
                    self.pipeline_id,
                    self.polls,
                )
                self.event.set()
                return


def build_status_reader(plugin: Any, pipeline_id: str) -> StatusReader | None:
    """从插件能力句柄构建状态读器；能力全缺席 → None（看门狗关闭）。

    主路径 `service-registry.pipeline-runs.list_by_pipeline`（精确按管道读 run
    簿记投影），兜底 `pipeline-state.list`（读 state 聚合行的 `run_status`）。
    两者都在内核 `STANDARD_CAPABILITIES` 内，sidecar 握手即拿句柄；任一缺席
    按能力缺失降级，不抛异常。
    """
    for capability_name, method in (
        (PRIMARY_CAPABILITY, PRIMARY_METHOD),
        (FALLBACK_CAPABILITY, FALLBACK_METHOD),
    ):
        try:
            handle = plugin.get_capability(capability_name)
        except Exception:  # noqa: BLE001 —— 能力未注入 = 看门狗关闭
            continue

        async def _read(
            handle: Any = handle,
            method: str = method,
        ) -> str | None:
            rows = await handle.call(method, {"pipeline_id": pipeline_id}, READ_TIMEOUT_SECS)
            return pick_row_status(rows, pipeline_id)

        return _read
    return None


def start_watcher(plugin: Any, call_context: Mapping[str, Any]) -> RunAbortWatcher | None:
    """按调用上下文装配看门狗（不启动）。返回 None = 本次调用不启用。

    启用条件全部满足才建：`_call_context` 带 pipeline_id（内核 tool-executor
    注入）+ 轮询间隔 > 0 + 状态读能力可用。任一不满足按关闭处理——看门狗是
    增强能力，缺一环不影响工具本体。
    """
    pipeline_id = str(call_context.get("pipeline_id", "") or "").strip()
    if not pipeline_id:
        return None
    poll_secs = resolve_poll_secs()
    if poll_secs <= 0:
        return None
    reader = build_status_reader(plugin, pipeline_id)
    if reader is None:
        logger.debug(
            "[bash_tool] 无 run 状态读能力（%s/%s），看门狗关闭",
            PRIMARY_CAPABILITY,
            FALLBACK_CAPABILITY,
        )
        return None
    return RunAbortWatcher(reader, poll_secs, pipeline_id)
