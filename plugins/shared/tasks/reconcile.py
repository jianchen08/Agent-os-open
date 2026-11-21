"""启动期运行簿记 × 任务投影分歧对账器（runs 退役收尾）。

域界定（ADR 2026-10-08-runs-retire-completion，翻案 ADR
2026-09-14-run-status-authority-startup-reconcile 的读面裁决权）：任务完成唯一
合法写面 = task_evaluate 评估闸门 + 评估闸门放行检测，「谁终态谁随写」；运行
簿记（state run_status 键，经 pipeline-runs.list 读出）只是执行实然记录，
**不构成对 task.status=completed 的裁决证据**——completed 侧分歧只发 WARNING
告警（事故 2026-09-28：簿记托底把熔断收尾的 run 裁成 completed 并向父管道
派发「已完成 ✅」——假完成不可有）。

failed 侧是本模块补写面之一（装机 2026-10-10 三连实证 B74/B75/B76）：进程
异常终止的 run 由内核启动清扫标 failed（reap_orphan_runs），但在途管道的
stop_check / task_reminder 失败写面从未执行、run.failed 域事件随进程死亡
丢失——投影恒卡 running，reconcile 每次装载重复告警却无人执笔。补写仅覆盖
「投影未决 × 簿记 failed」：failed 无假成功风险（不触 09-28 红线），且候选
已按真最新 run 收敛（新在飞 run 在场不写）。owned 镜像已退役
（ADR 2026-11-02-state-key-governance）：无父侧镜像纠正面，父会话面板
join 子行取真值。

用户停止侧同为补写面（缺陷 I，R2 移动端 2026-10-15 实证）：停止写面自带
随写，但垂死 run 的步边界 stale 投影（内核 per-step 投影逐键 upsert
run 本地 state 旧值）会把随写覆写回 running——投影三面一致钉死 running，
僵尸任务永久占据面板运行视图，reconcile 重复告警无人执笔（与 failed 侧
同构）。补写仅覆盖「投影未决 × 簿记 cancelled」（簿记 cancelled = 内核
run 域的用户停止词，照旧），词汇 = stopped（ADR 2026-10-30 词汇收敛：
任务域 cancelled 退役，用户停止 = stopped），不写 failure_class（停止是
用户裁决不是失败，与停止写面同形状）；任务投影 stopped/timeout 等用户侧
可重入态不构成分歧，处置权仍归用户侧。

三分刀（ADR 2026-10-30 §4.4，词汇收敛收尾）：

- 排空对账：run 簿记 suspended × 任务投影 running → 写 task.status=stopped
  （重启排空遗留：G8 只把 run 翻 suspended、从不写任务键，投影停在 running
  与簿记脱钩——实证事故链起点）；
- 僵尸告警：任务投影真终态（completed/failed）× run 簿记非终态
  （running/suspended）→ 纯 logger.warning 不裁决。**写不了也不该写**：
  本插件 manifest state.writes 只声明 task.* 与 run.plugin_errors 单键
  （后者 = 第八刀界内收敛唯一 run 侧出口；run 簿记键不在声明面，双状态裁定
  「run_status 内核持有、插件不可写」的机械执法），run 簿记
  残留属内核域，由该管道下一次 run 的 init/finalize 自然重写收敛；
- 历史迁移：任务投影 cancelled（含历史 canceled 双拼写）存量行 → stopped
  （一次性开机通过，词汇退役收尾）。

分歧行识别（纯函数 classify_divergence，词汇承自旧仲裁矩阵便于对照）：

- 投影 completed × 簿记 failed/suspended → 投影持完成证据而簿记分歧
  （stop→suspended→收尾实录形态：收尾写失败后簿记停留 suspended）。
- 投影真终态（completed/failed）× 簿记 running/suspended →
  `zombie_terminal_projection`（僵尸告警，纯告警不裁决，见上）。
- 投影 running × 簿记 suspended → `suspended_run_drain`（排空对账，
  补写 stopped）。
- 投影未决 × 簿记 failed/cancelled → 运行已终而投影未决（kill 方未随写形态）。
- 投影未决 × 簿记 completed → run 正常收尾而评估证据未落（事故 09-28 形态；
  完成唯一判据 = 评估通过，用户裁定重申——run 正常收尾不构成完成证据）。
- 投影未决 × 簿记 completed × 该 run 留有插件级失败 → `plugin_errors_unrecorded`
  （ADR 2026-10-17）：内核收尾把 `_plugin_errors` 镜像成常驻键
  `run.plugin_errors`（前者是引擎私有 staging 键，落库前被剥离，重启即失忆）。
  有了它，真实成因不再被误报成"评估证据未落"。**纯告警不裁决**：ADR
  2026-10-15 已定「有产出 + 步失败 = completed + degraded」为合法完成态，
  本模块不改其语义，也不抢占簿记终态（failed/cancelled）的补写面。
- 真最新 run = running/suspended 不告警（在飞 run 与幽灵无法单次扫描区分；
  候选集含 running，「旧终态 run + 新在飞 run」按真最新判定不误报）。

补写面身份守卫（`_is_task_pipeline_row`）：补写只落任务管道——行含
`lineage.*` 血缘键才算任务管道（子任务与根任务出生即带，会话管道恒无，
agent_config_load 以同一判据写 task.id）。装机 2026-10-21 实锤：会话行被
误判成任务行后，其**自身会话 run** 的取消簿记被补写成会话的
task.status=旧停止词（彼时豁免署名、新 run 复活不复位），会话管道自此
每个带工具调用的轮次被 autonomous post 终态路由截杀
（dropped_tool_calls 死锁）。守卫双重：缺血缘键的行不进分歧判定（补写是
绕过存在性仲裁的修复路径，必须自带身份判别）；其中带 task.status 投影的
（旧版误写残留）在扫描期清键复位（`_clear_task_status_residue`，null =
state 清键）——该残留被 classify 视为用户终态不构成分歧，豁免署名又令
新 run 不复位，只能在此按身份显式清扫。

扫描触发点 = task_service on_load（插件服务可用时）；插件热重载（运行中
respawn）同路径安全。任一能力读面故障 → 降级留痕返回空（启动扫描是观测面，
不阻断插件装载）。

未回执补发面（ADR 2026-10-28-task-terminal-notify-loss-and-reopen-evidence
D2，装机 2026-10-28 事故）：终态域事件是**广播即发**（内核 DomainEvent →
已装载的 domain_event 订阅者），订阅插件被空闲卸载/宿主重载的窗口里事件被
静默丢弃（无重放、无死信）——上级既收不到「子任务已完成」通知，父管道
``task.subtasks_pending.<子管道 id>`` 回执也从未写过（终态回执面与事件派发
同批，事件丢了回执也没了）。本面是那条收敛面：子任务自身投影已是任务域终态
（``task.status ∈ {completed, failed}``——证据源是任务域自己的投影，**不是**
runs 簿记，ADR 2026-10-20 已否「拿第二真值补写终态」）**且**父管道仍持有
非空挂号键 → 重发那一轮已落定的终态事件 + 回执 + 纠正父侧 owned 脏镜像。

本面**不改写任何终态词汇**（不写子任务的 task.status / task.ended_at——那是
评估闸门 task_evaluate 的写面），只让「已落定的终态」重新走一遍事件派生路径。
幂等锚 = 挂号键清空（同一 (父,子) 只可能补发一次；已正常送达过的终态事件
挂号早已清空，不会重复补发）。

时效窗（ADR 2026-10-28 D2-1，装机实测追加）：补发只覆盖**上一轮工作窗口内
漏发**的候选——终态时刻距今 ≤ 窗口（默认 24h）才补发。超窗候选 = 历史积压
（装机实测：父管道挂着 9 条跨 10-19~10-28 的未回执挂号），一次性倒给上级 =
噪声（上周的旧事被当新消息处理、冲掉当前会话上下文）→ 不补发、**不清回执**
（清了就永远没人知道它漏过），只留 WARNING 痕转人工治理（可观测优先）。
终态时刻取子管道投影的 ``task.ended_at``；缺失时回退父管道登记时间
（``task.subtasks_pending.<id>`` 的值 = 提交时间戳，**取旧** → 判定更保守）。
窗口可配：环境变量 ``AGENTOS_TASK_TERMINAL_REPLAY_WINDOW_HOURS``（小时）。
"""

from __future__ import annotations

import logging
import os
import sys
import time as _time
from datetime import UTC, datetime
from typing import Any

import events

# 共享层自举（service_access 同约定）：_root_api 解析不依赖调用方先垫
# sys.path——孤跑/组合车道差异下，模块缓存未暖时裸导入会炸（test_tasks_gaps
# 孤跑 _root_api 路径伪影，两轮案底）。本文件在 plugins/shared/tasks/ 下，
# 共享根 = 上一层（写 ..\.. 会落到 plugins，_root_api 永不可达）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from _root_api import state_fields  # noqa: E402 — 共享层自举后导入（本文件头同款）

logger = logging.getLogger(__name__)

# 投影 completed（完成证据在场）时构成分歧的簿记侧状态
_DIVERGENT_RUN_STATUSES_FOR_COMPLETED = frozenset({"failed", "suspended"})

# "无终态证据"集（未决投影）——仅这些值才进入分歧识别；用户侧可重入态
# （stopped/timeout）与任务域终态（completed/failed）不是分歧。
_UNDECIDED_TASK_STATUSES = frozenset({"", "running", "pending", "evaluating"})

# 僵尸告警判据（三分刀之二，ADR 2026-10-30 §4.4）：任务投影真终态 × run
# 簿记非终态 → 纯告警不裁决（run 簿记残留属内核域，插件写不了也不该写）。
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed"})
_ZOMBIE_RUN_STATUSES = frozenset({"running", "suspended"})

# 历史迁移刀（三分刀之三）：任务投影旧词汇 cancelled（含历史写入方的
# canceled 双拼写）存量行 → stopped，一次性开机通过。
_LEGACY_CANCELLED_TASK_STATUSES = frozenset({"cancelled", "canceled"})

# 非任务管道行的残留清扫谓词：任意非空 task.status 值都是残留。会话行
# （缺血缘键）不应携带任何 task.status——值在即有人误写，不区分终态/未决：
# 未决值 'running' 由 run-started 复活复位误落在会话上后永不复位（复位目标
# 就是 running，自身幂等不再写也不清），面板伪任务行标题兜底成裸管道 ID
# （2026-10-27 用户报告装机实证：8bde3343a8bb 残留 running + 3 主管道
# null 墓碑）。终态误写形态（2026-10-21 装机死锁：autonomous post 终态路由
# 截杀面）是本谓词的子集，一并覆盖。
# 空值（None/''）不进清扫域：那是清扫自身的产物（null 墓碑 = 清除终态），
# 判据（is_task_state 值语义）已不再命中，重复 null 写纯负载。

# 候选扫描 = 簿记五态全量按 status 拉取（单次 500 上限），候选按管道收敛到
# 真最新 run（含 running——在飞 run 参与最新性收敛，防「旧终态候选 + 新在飞
# run」被误报）
_SCAN_RUN_STATUSES = ("running", "suspended", "failed", "completed", "cancelled")

# failed 侧补写的失败归因：原样透传 run 署名，不猜测（评估体系全景 §10.2
# 「复盘分诊只认终局归因声明，不从错误堆反推」——本模块是 kill 方未随写时的
# 归因执笔者，写猜测文案即等于伪造归因）。
#
# 事故 2026-10-17：墙钟超时（run_stop_reason=timeout）被一律报成「运行进程
# 异常终止（run 簿记由启动清扫标记 failed）」，而当日唯一的启动清扫在 6 小时
# 前、reap 的是另一批 run——归因与事实无关，把排查方向带偏。
_FAILURE_CLASS_RUN_SIGNED = "run_stopped"
_FAILURE_CLASS_RUN_ABORTED = "run_aborted"

# 内核契约停机词（core::types::RunStatus::FAILED_STOP_REASONS）→ 人话。
# 插件经 manifest capabilities.stop_reasons 声明的署名词不在此表——不猜其
# 语义，原词透传（归因以「知道是谁署的」为准，不以「说得像」为准）。
# 例外：duplicate_check 声明的 model_unavailable / llm_fail_loop 两词是本域
# 掌握语义的第一方署名（ADR 2026-10-20-model-hot-swap-inflight-guard），收录
# 入表给出如实中文归因。
_SIGNATURE_REASONS: dict[str, str] = {
    "timeout": "墙钟预算耗尽，引擎强制终态化",
    "max_iterations": "轮次预算耗尽",
    "budget_exhausted": "预算耗尽",
    "elapsed_cap": "时长上限触发",
    "iteration_cap": "轮次上限触发",
    "stalled": "执行停滞",
    "duplicate_loop": "重复循环",
    "tool_fail_loop": "工具连续失败循环",
    "dropped_tool_calls": "工具调用被丢弃",
    "empty_reply_exhausted": "空回复耗尽",
    "task_failed": "任务判定失败",
    "model_unavailable": "模型不可用（配置面缺失/未命中，拒绝静默回落）",
    "llm_fail_loop": "LLM 连续同类失败循环",
}


def failure_attribution(run_stop_reason: str) -> tuple[str, str]:
    """run 终态署名 → (failure_class, failure_reason)。

    签名即证据：有署名就原样带出（词表内译人话，词表外原词透传），没有署名
    就如实说「未记录」——**不得用任何具体成因回填**（进程异常终止、启动清扫
    都只是猜测，未署名形态下二者都可能，也可能都不是）。

    Args:
        run_stop_reason: 内核终态落库键 ``run_stop_reason``（终局签名；空串
            = 未署名）。跨 run 每次终态都写，无上一轮残留。

    Returns:
        ``(failure_class, failure_reason)`` 元组。
    """
    word = (run_stop_reason or "").strip()
    if not word:
        return (
            _FAILURE_CLASS_RUN_ABORTED,
            "运行终止但未记录原因（run 簿记 failed 且无 stop_reason 署名）",
        )
    described = _SIGNATURE_REASONS.get(word)
    detail = described if described else f"署名 {word}（插件声明停机词）"
    return (_FAILURE_CLASS_RUN_SIGNED, f"运行终止（{detail}）")

# 用户停止侧补写词汇（ADR 2026-10-30 词汇收敛）：任务域 cancelled 退役，
# 用户停止 = stopped（与等子任务/重启排空三态同词；不写 failure_class——
# 停止是用户裁决不是失败）
_USER_STOP_TERMINAL = "stopped"

# 第五刀（ADR 2026-11-02-state-key-governance）：无 run 证据残留的未决值域
# （running/pending；终态与 evaluating 不在列——evaluating 时主管道 run 在跑，
# 必有 run_id 键）
_NO_RUN_EVIDENCE_PENDING_STATUSES = ("running", "pending")

# 未回执补发面（ADR 2026-10-28 D2）：任务域终态投影词汇 → 触发派生的 run
# 终态事件名。补发走与在线路径**同一条**派生构造（events.derive_task_terminal_events），
# 不另造一套标签拼装。
_TERMINAL_REPLAY_RUN_EVENTS: dict[str, str] = {
    "completed": "run.completed",
    "failed": "run.failed",
}

_CRASH_SWEEP_REASON = "crash_swept"
"""内核崩溃清扫署名（reap_orphan_runs）。

带此署名的 failed run 属“重启打断”而非真失败：reconcile **不投影**
task.status=failed——任务保持 running，由 tasks boot 自动恢复
（auto_resume_drained_tasks）拉起续跑轮；拉起失败才写 failed
（failure_class=relaunch_failed）。ADR 2026-11-13-crash-swept-no-failed-task-projection。
"""

# 未回执补发面的时效窗（ADR 2026-10-28 D2-1）：只有「上一轮工作窗口内漏发」
# 的候选才补发；更早的漏发属历史积压 → 转人工治理（既不通知也不静默清回执）。
#
# 配置：环境变量 AGENTOS_TASK_TERMINAL_REPLAY_WINDOW_HOURS（小时，默认 24）。
#   - 合法正数 → 该值为窗；
#   - 不可解析 → 回默认 24h 并留痕（配置坏掉不该改变默认行为）；
#   - ≤ 0 → **显式关闭补发面**（不回发任何终态事件；候选仍留 WARNING 痕）——
#     「关掉就一个都不补」比「回落到默认窗」更确定（宁可不补也不制造噪声）。
_REPLAY_WINDOW_ENV = "AGENTOS_TASK_TERMINAL_REPLAY_WINDOW_HOURS"
_DEFAULT_REPLAY_WINDOW_HOURS = 24.0


def classify_divergence(
    run_status: str, task_status: str, plugin_error_count: int = 0
) -> str | None:
    """分歧行识别纯函数：簿记状态 × 投影终态 × 插件级失败 → 告警类别。

    Args:
        run_status: 真最新 run 的簿记状态。
        task_status: 任务投影终态。
        plugin_error_count: 该 run 留下的插件级失败条数（内核收尾常驻镜像
            ``run.plugin_errors``）。ADR 2026-10-15 已定「有产出 + 步失败 =
            degraded」为合法完成态，故本参数只用于**告警类别选词**，不改任何
            裁决语义（无补写、无裁 completed）。

    Returns:
        "projection_completed_run_diverged"（投影 completed 持完成证据 × 簿记
        failed/suspended）/ "zombie_terminal_projection"（投影真终态
        completed/failed × 簿记 running/suspended：僵尸告警，纯告警不裁决
        ——run 簿记属内核域，插件写不了也不该写）/ "suspended_run_drain"
        （投影 running × 簿记 suspended：排空对账，补写 stopped）/
        "undecided_run_terminal"（未决投影 × 簿记 failed/cancelled：kill 方
        未随写形态）/ "run_completed_evaluation_missing"（未决投影 × 簿记
        completed：run 正常收尾而评估证据未落，事故 2026-09-28 形态）/
        "plugin_errors_unrecorded"（未决投影 × 簿记 completed × 该 run
        有插件级失败：重启后插件级失败不再失忆，真实成因不再被误报成"评估未落"
        ——ADR 2026-10-17）/ None（一致行、在飞行、可恢复行、用户侧可重入态、
        废弃值残行——不告警）。

    插件级失败面不抢占簿记终态的补写面（failed/用户停止侧 healing 比告警更
    值钱）：失败事实改由常驻键 run.plugin_errors 在面板/日志面可见。
    """
    if task_status == "completed" and run_status in _DIVERGENT_RUN_STATUSES_FOR_COMPLETED:
        return "projection_completed_run_diverged"
    if task_status in _TERMINAL_TASK_STATUSES and run_status in _ZOMBIE_RUN_STATUSES:
        return "zombie_terminal_projection"
    if task_status == "running" and run_status == "suspended":
        return "suspended_run_drain"
    if task_status in _UNDECIDED_TASK_STATUSES:
        if run_status in ("failed", "cancelled"):
            return "undecided_run_terminal"
        if run_status == "completed":
            if plugin_error_count > 0:
                return "plugin_errors_unrecorded"
            return "run_completed_evaluation_missing"
    return None


def _is_task_pipeline_row(row: dict[str, Any]) -> bool:
    """补写面任务管道判别：统一判据（events.is_task_pipeline_row，ADR
    2026-11-06 D1）——``task.id`` 非空 ∨ 任一 ``lineage.*`` 键非空。

    任务管道出生即带身份锚（task.id 非空 + 血缘键），会话管道恒无。旧口径
    （"仅 lineage.* 键存在"）漏判 534ef18c1ba8 型真任务（有 task.id 无
    血缘键）——统一后它们正常参与分歧判定与补发面。补写面绕过存在性仲裁
    （cancel 写面 404 门不覆盖本路径），身份必须在此自证；is_task_state 只
    保证"像任务行"，会话携带 task.owned.* 记账键时两者结论可能分歧，补写以
    身份判据为准。
    """
    return events.is_task_pipeline_row(row)


# 残留清扫键集（D5 N9 审计，ADR 2026-11-06）：非任务管道行不得携带任何
# task.* 自身键——失败注记（error/failure_class/failure_reason）不清会让
# 会话永久命中 is_task_state（伪任务行永不退场，清扫自激循环）。
_RESIDUE_CLEAR_KEYS = (
    "task.status",
    "task.ended_at",
    "task.error",
    "task.failure_class",
    "task.failure_reason",
)


async def _clear_task_status_residue(
    state_cap: Any, pipeline_id: str, residue: str
) -> bool:
    """清扫非任务管道行的 task.* 自身键残留（全键集置 null）。

    误写残留含终态署名（旧停止词 cancelled，2026-10-21 死锁形态）、未决值
    （running，run-started 复活复位落下，2026-10-27 主管道裸 ID 形态）与
    失败注记（error/failure_class/failure_reason，2026-11-06 审计键集扩展）
    。null = state 清键，与 resume overlay 清失败注记同写法。写失败仅告警
    留痕（下轮装载重试）。
    """
    fields: dict[str, Any] = dict.fromkeys(_RESIDUE_CLEAR_KEYS, None)
    try:
        await state_cap.call(
            "update",
            {"pipeline_id": pipeline_id, "fields": fields},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] 非任务管道终态残留清扫失败（降级留痕，下轮装载重试）"
            "| pipeline=%s | residue=%s | err=%s",
            pipeline_id, residue, exc,
        )
        return False
    logger.info(
        "[task_reconcile] 非任务管道终态残留清扫完成 | pipeline=%s | residue=%s",
        pipeline_id, residue,
    )
    return True


async def reconcile_startup(
    state_cap: Any, runs_cap: Any, bus_cap: Any | None = None
) -> list[dict[str, Any]]:
    """扫描分歧行：failed/用户停止侧补随写，completed 侧与僵尸形态纯告警，
    旧词汇行历史迁移；并补发「父管道挂号未回执」的漏发终态事件。返回对账清单。

    数据面：pipeline-state.list（含 DB 冷兜底，重启后投影可见）×
    pipeline-runs.list（簿记五态全量按 status 过滤）。每个候选管道取真最新
    run 判定（多 run 历史：旧 failed + 新 completed = 合法重跑，不告警；在飞
    run 参与最新性收敛，最新 run = running/suspended 不告警也不补写——排空
    对账刀除外：投影 running × 簿记 suspended 恰是重启排空遗留，补写 stopped）。

    补写面 = 未决投影 × 簿记终态（failed/用户停止，见模块 docstring）：两者
    均无假成功风险；写失败降级留痕（观测面不阻断装载）。completed 侧零写零派发。
    历史迁移刀不需要簿记读面（旧词汇行按投影直迁）。

    未回执补发面（ADR 2026-10-28 D2）与簿记分歧面**并列且互不牵连**：补发判据
    只认任务域自己的投影终态 + 父管道挂号面，不依赖运行簿记读面——簿记读面故障
    不阻断补发，event-bus 能力缺席也只降级补发面自身（回执 + 父镜像照写）。

    Args:
        state_cap: pipeline-state 能力句柄（读面 + update 写面）。
        runs_cap: service-registry 能力句柄（pipeline-runs.list 簿记读面）。
        bus_cap: event-bus 能力句柄（补发面 emit_domain）；缺席（None）时补发面
            降级为「只回执 + 父镜像」。可选参数——既有调用方与既有测试不破。
    """
    try:
        rows = await state_cap.call("list", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[task_reconcile] 能力读面缺席/故障，启动对账降级跳过 | err=%s", exc)
        return []
    if not isinstance(rows, list):
        return []

    # 未回执补发面（与既有补写面并列；独立于 runs 簿记读面）
    replayed = await _replay_unreceipted_terminal_events(state_cap, rows, bus_cap)

    # 历史迁移刀（三分刀之三，ADR 2026-10-30 §4.4）：任务投影旧词汇
    # cancelled（含 canceled 双拼写）存量行 → stopped，一次性开机通过。
    # 独立预扫、先于簿记分歧面：迁移与 run 无关，簿记读面故障不牵连迁移；
    # 迁移后的行按 stopped 干净参与后续判定（stopped 可重入，不进分歧集）。
    divergences: list[dict[str, Any]] = await _migrate_legacy_cancelled_rows(
        state_cap, rows
    )
    # 第七刀（ADR 2026-11-02-state-key-governance）：timeout 死枚举值存量行
    # → failed（同迁移刀形态，簿记读面无关）。
    divergences.extend(await _migrate_legacy_timeout_rows(state_cap, rows))
    # 第八刀（ADR 2026-11-17-bounded-error-ledger-and-iteration-cow P4 延伸）：
    # 遗留 run.plugin_errors 超界行收敛（有界账本只管新写，存量行无收缩机制，
    # 前端快照每轮原样拖出）。键级机械收敛，不设任务身份守卫（引擎镜像键）。
    divergences.extend(await _migrate_legacy_plugin_error_rows(state_cap, rows))
    # 第六刀（ADR 2026-11-02-state-key-governance）：幽灵 owned 登记清理
    # （子管道行已不存在的登记键与挂号键回收；读面无关，全量行即判据）。
    divergences.extend(await _sweep_ghost_owned_registrations(state_cap, rows))

    try:
        runs: list[dict[str, Any]] = []
        for status in _SCAN_RUN_STATUSES:
            page = await runs_cap.call("pipeline-runs.list", {"status": status, "limit": 500})
            if isinstance(page, list):
                runs.extend(r for r in page if isinstance(r, dict))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] 运行簿记读面缺席/故障，簿记分歧面降级跳过"
            "（补发面与迁移刀不受牵连） | err=%s",
            exc,
        )
        return divergences + replayed

    # 候选行按 pipeline_id 收敛到最新 run（created_at 字典序 = ISO 时间序）
    candidates: dict[str, dict[str, Any]] = {}
    for run_row in runs:
        pid = str(run_row.get("pipeline_id") or "")
        if not pid:
            continue
        prev = candidates.get(pid)
        if prev is None or str(run_row.get("created_at") or "") > str(prev.get("created_at") or ""):
            candidates[pid] = run_row

    for row in rows:
        if not isinstance(row, dict) or not events.is_task_state(row):
            continue
        pid = str(row.get("pipeline_id") or "")
        # 身份守卫（补写面 + 清扫面）：缺血缘键 = 非任务管道（会话）——
        # 分歧判定前先分流。带 task.status 残留的清扫复位（误写形态：
        # 用户侧豁免署名、run-started 复活复位落下的未决值 running——
        # 不扩身份判据、按值清扫，见 _RESIDUE 清扫谓词注释 2026-10-27；
        # classify 视为用户终态/未决态不构成分歧——只能在此按身份显式清，
        # 否则会话被 autonomous post 终态路由持续截杀，dropped_tool_calls
        # 死锁，2026-10-21 装机实锤）；无残留（空值）的行不在本模块裁决域，
        # 静默跳过。
        if not _is_task_pipeline_row(row):
            # 清扫触发 = 任一残留自键非空（is_task_state 命中即伪任务行——
            # 仅 task.status 触发的旧口径漏掉 error-only 残留形态）
            residue = next(
                (
                    str(row.get(key))
                    for key in _RESIDUE_CLEAR_KEYS
                    if row.get(key) is not None and row.get(key) != ""
                ),
                "",
            )
            if residue:
                cleared = await _clear_task_status_residue(state_cap, pid, residue)
                divergences.append(
                    {
                        "pipeline_id": pid,
                        "kind": "non_task_residue_cleared",
                        "residue": residue,
                        "residue_cleared": cleared,
                    }
                )
                logger.warning(
                    "[task_reconcile] 非任务管道终态残留清扫（误写形态）"
                    "| pipeline=%s | residue=%s | cleared=%s",
                    pid, residue, cleared,
                )
            continue
        task_id = str(row.get("task.id") or "")
        task_status = str(row.get("task.status") or "")
        run = candidates.get(pid)
        if run is None:
            # 第五刀（ADR 2026-11-02-state-key-governance）：无 run 证据的未决
            # 残留收束——提交后、执行起手前崩溃（或 run 簿记从未落库）会让投影
            # 永久悬挂（ADR 实录 14 条规则外残留）。行内 run_id 键缺席 = 引擎
            # persist_run_start 从未落库（有键则可能是五态分页截断漏扫，宁可
            # 漏裁不误裁）。裁决 stopped（可恢复语义）+ run_aborted 归因；
            # 不托底 completed（09-28 红线）。幂等：写后投影 stopped 不再命中
            # 未决集。
            if task_status in _NO_RUN_EVIDENCE_PENDING_STATUSES and not row.get("run_id"):
                followed = await _follow_write_stopped_no_run_evidence(
                    state_cap, pid, task_id
                )
                divergences.append(
                    {
                        "pipeline_id": pid,
                        "task_id": task_id,
                        "kind": "no_run_evidence_stopped",
                        "task_status": task_status,
                        "followed_write": followed,
                    }
                )
                logger.warning(
                    "[task_reconcile] 无 run 证据未决残留收束（第五刀）"
                    "| pipeline=%s | task=%s | 投影=%s | followed=%s",
                    pid, task_id, task_status, followed,
                )
            continue
        run_status = str(run.get("status") or "")
        plugin_error_count = _plugin_error_count(row)
        kind = classify_divergence(run_status, task_status, plugin_error_count)
        if kind is None:
            continue
        entry = {
            "pipeline_id": pid,
            "task_id": task_id,
            "run_id": run.get("run_id"),
            "run_status": run_status,
            "task_status": task_status,
            "kind": kind,
        }
        if kind == "undecided_run_terminal" and run_status in ("failed", "cancelled"):
            if run_status == "cancelled":
                entry["followed_write"] = await _follow_write_user_stop(
                    state_cap, pid, run, task_id
                )
            elif str(row.get("run_stop_reason") or "") == _CRASH_SWEEP_REASON:
                # 崩溃清扫行不投影 failed：任务保持 running，boot 自动恢复直接
                # 拉起续跑轮；只有拉起失败才由 auto_resume 写 failed。
                entry["deferred"] = "crash_swept_auto_resume"
                logger.info(
                    "[task_reconcile] 崩溃清扫行不投影 failed（等自动恢复拉起）"
                    "| pipeline=%s | task=%s",
                    pid,
                    task_id,
                )
            else:
                entry["followed_write"] = await _follow_write_failed(
                    state_cap, pid, run, task_id, str(row.get("run_stop_reason") or "")
                )
            # owned 镜像已退役（ADR 2026-11-02-state-key-governance）：父会话
            # 面板 join 子行真值，无镜像可随写。
        if kind == "suspended_run_drain":
            # 排空对账刀（三分刀之一）：投影 running × 簿记 suspended →
            # 写 stopped（重启排空遗留）。owned 镜像退役，无父侧写面。
            entry["followed_write"] = await _follow_write_stopped_drain(
                state_cap, pid, task_id
            )
        # zombie_terminal_projection：纯告警不裁决（无任何写面）——run 簿记
        # 残留属内核域，由该管道下一次 run 的 init/finalize 自然重写收敛。
        if kind == "plugin_errors_unrecorded":
            entry["plugin_error_count"] = plugin_error_count
        divergences.append(entry)
        logger.warning(
            "[task_reconcile] 运行簿记与任务投影分歧（%s）"
            "| kind=%s | pipeline=%s | task=%s | 簿记=%s | 投影=%s | 插件级失败=%s",
            f"{run_status} 侧已补随写" if entry.get("followed_write") else "纯告警不裁决：谁终态谁随写",
            kind, pid, entry["task_id"], run_status, task_status,
            plugin_error_count or "-",
        )
    divergences.extend(replayed)
    return divergences


def _plugin_error_count(row: dict[str, Any]) -> int:
    """state 行的插件级失败条数（内核收尾常驻镜像 ``run.plugin_errors``）。

    跨边界可能落成 JSON 字符串（state_fields 契约）——按 ``as_list`` 还原；
    形态非法按缺失处理（0 条），不因读面异常让整个对账降级。
    """
    errors = state_fields.as_list(row.get("run.plugin_errors"), field="run.plugin_errors")
    return len(errors) if errors else 0


async def _follow_write_failed(
    state_cap: Any,
    pipeline_id: str,
    run: dict[str, Any],
    task_id: str,
    run_stop_reason: str,
) -> bool:
    """failed 侧补随写（唯一补写面）：投影未决 × 簿记 failed → task 终态 failed。

    字段词表对齐在途失败写面（task_reminder/stop_check）：status/ended_at/
    failure_class/failure_reason。ended_at 取 run 簿记值（run 终止时刻）。
    failure_class/failure_reason 经 failure_attribution 从 run 署名原样透传
    ——本模块不产成因，只搬运内核已署名的成因。写失败仅告警留痕。
    """
    failure_class, failure_reason = failure_attribution(run_stop_reason)
    fields: dict[str, Any] = {
        "task.status": "failed",
        "task.failure_class": failure_class,
        "task.failure_reason": failure_reason,
    }
    ended_at = str(run.get("ended_at") or "")
    if ended_at:
        fields["task.ended_at"] = ended_at
    try:
        await state_cap.call("update", {"pipeline_id": pipeline_id, "fields": fields})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] failed 侧补随写失败（降级留痕，下轮装载重试）"
            "| pipeline=%s | task=%s | err=%s",
            pipeline_id, task_id, exc,
        )
        return False
    logger.info(
        "[task_reconcile] failed 侧投影补随写完成 | pipeline=%s | task=%s | "
        "failure_class=%s",
        pipeline_id, task_id, _FAILURE_CLASS_RUN_ABORTED,
    )
    return True


async def _follow_write_user_stop(
    state_cap: Any, pipeline_id: str, run: dict[str, Any], task_id: str
) -> bool:
    """用户停止侧补随写（缺陷 I，R2 移动端实证）：投影未决 × 簿记 cancelled
    → task.status = stopped。

    簿记 cancelled = 内核 run 域的用户停止词（照旧）；任务域词汇 = stopped
    （ADR 2026-10-30 词汇收敛：cancelled 退役，用户停止 = stopped）。
    不写 failure_class（停止是用户裁决不是失败，与停止写面同形状：
    status + ended_at）。ended_at 取 run 簿记值。写失败仅告警留痕
    （下轮装载重试）。"""
    fields: dict[str, Any] = {"task.status": _USER_STOP_TERMINAL}
    ended_at = str(run.get("ended_at") or "")
    if ended_at:
        fields["task.ended_at"] = ended_at
    try:
        await state_cap.call("update", {"pipeline_id": pipeline_id, "fields": fields})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] 用户停止侧补随写失败（降级留痕，下轮装载重试）"
            "| pipeline=%s | task=%s | err=%s",
            pipeline_id, task_id, exc,
        )
        return False
    logger.info(
        "[task_reconcile] 用户停止侧投影补随写完成（stopped）| pipeline=%s | task=%s",
        pipeline_id, task_id,
    )
    return True


async def _follow_write_stopped_drain(
    state_cap: Any, pipeline_id: str, task_id: str
) -> bool:
    """排空对账刀（三分刀之一）：投影 running × 簿记 suspended → 写 stopped。

    重启排空遗留：G8 优雅重启只把 run 翻 suspended、从不写任务键——投影
    停在 running 与簿记脱钩（实证事故链起点）。写 stopped = 暂停放那里
    （可恢复，与用户停止同词）；不写 ended_at（排空不是终局，挂起 run 的
    续跑轮经 run-started 复位自然回 running）。写失败仅告警留痕
    （下轮装载重试）。"""
    try:
        await state_cap.call(
            "update",
            {"pipeline_id": pipeline_id, "fields": {"task.status": _USER_STOP_TERMINAL}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] 排空对账写失败（降级留痕，下轮装载重试）"
            "| pipeline=%s | task=%s | err=%s",
            pipeline_id, task_id, exc,
        )
        return False
    logger.info(
        "[task_reconcile] 排空对账完成：任务投影复位 stopped | pipeline=%s | task=%s",
        pipeline_id, task_id,
    )
    return True


async def _migrate_legacy_cancelled_rows(
    state_cap: Any, rows: list[Any]
) -> list[dict[str, Any]]:
    """历史迁移刀（三分刀之三）：任务投影 cancelled/canceled 存量行 → stopped。

    词汇退役收尾（ADR 2026-10-30）：任务域无 cancelled（用户停止 = stopped），
    存量旧词行开机迁移后按 stopped 干净参与后续判定（可重入、可自然唤醒）。
    独立预扫、先于簿记分歧面：迁移与 run 簿记无关，簿记读面故障不牵连迁移。
    只迁移任务管道行（``is_task_state`` + ``_is_task_pipeline_row`` 双守卫）；
    会话行的旧词残留走 :func:`_clear_task_status_residue` 清键面（不迁移——
    会话本无任务状态）。迁移不触碰 task.ended_at（终态时刻是事实，不在词汇
    迁移域）。写失败仅告警留痕（下轮装载重试），清单如实带 migrated 标记。

    Returns:
        迁移清单（每条含 pipeline_id/task_id/kind/legacy/migrated）。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not events.is_task_state(row):
            continue
        if not _is_task_pipeline_row(row):
            continue
        pid = str(row.get("pipeline_id") or "")
        task_id = str(row.get("task.id") or "")
        legacy = str(row.get("task.status") or "")
        if legacy not in _LEGACY_CANCELLED_TASK_STATUSES:
            continue
        migrated = False
        try:
            await state_cap.call(
                "update",
                {"pipeline_id": pid, "fields": {"task.status": _USER_STOP_TERMINAL}},
            )
            migrated = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[task_reconcile] 历史迁移写失败（降级留痕，下轮装载重试）"
                "| pipeline=%s | task=%s | legacy=%s | err=%s",
                pid, task_id, legacy, exc,
            )
        else:
            logger.info(
                "[task_reconcile] 历史迁移完成 | pipeline=%s | task=%s | %s → stopped",
                pid, task_id, legacy,
            )
        logger.warning(
            "[task_reconcile] 历史迁移：任务投影旧词汇退役 → stopped"
            "| pipeline=%s | task=%s | legacy=%s | migrated=%s",
            pid, task_id, legacy, migrated,
        )
        out.append(
            {
                "pipeline_id": pid,
                "task_id": task_id,
                "kind": "legacy_cancelled_migrated",
                "legacy": legacy,
                "migrated": migrated,
            }
        )
    return out


async def _follow_write_stopped_no_run_evidence(
    state_cap: Any, pipeline_id: str, task_id: str
) -> bool:
    """第五刀写面（ADR 2026-11-02-state-key-governance）：无 run 证据的未决
    残留 → stopped + run_aborted 归因。

    提交后、执行起手前崩溃（或 run 簿记从未落库）→ 投影永久悬挂（ADR 实录
    14 条规则外残留）。裁决 stopped（可恢复语义，与排空刀同词）+
    failure_class=run_aborted（无 run 署名可搬运，与 undecided_run_terminal
    无署名同款归因）；不写 ended_at（从未真正起跑，无终局时刻可言）。
    不托底 completed（09-28 红线）。写失败仅告警留痕（下轮装载重试）。"""
    try:
        await state_cap.call(
            "update",
            {
                "pipeline_id": pipeline_id,
                "fields": {
                    "task.status": _USER_STOP_TERMINAL,
                    "task.failure_class": _FAILURE_CLASS_RUN_ABORTED,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] 无 run 证据残留收束写失败（降级留痕，下轮装载重试）"
            "| pipeline=%s | task=%s | err=%s",
            pipeline_id, task_id, exc,
        )
        return False
    logger.info(
        "[task_reconcile] 无 run 证据残留收束完成（stopped + run_aborted）"
        "| pipeline=%s | task=%s",
        pipeline_id, task_id,
    )
    return True


async def _sweep_ghost_owned_registrations(
    state_cap: Any, rows: list[Any]
) -> list[dict[str, Any]]:
    """第六刀：幽灵 owned 登记清理（ADR 2026-11-02-state-key-governance）。

    提交者管道上的 ``task.owned.<子管道 id>.*`` 登记键与
    ``task.subtasks_pending.<子管道 id>`` 挂号键，在子管道行已不存在（任务
    已删除/从未出生）时是永存幽灵——装机 2026-11-02 实证两条（全库零记录
    仍标 running，误导读面）。判据：owned_id 不在全量行 pipeline_id 集合。
    处置：owned 各键 + 挂号键写 null（state 清键语义；含 24h 超窗积压挂号
    ——子行不存在则补发无对象，回收）。幂等：全 null 墓碑不重复写。
    owned.status 已退役（同 ADR），存量 status 键一并回收。
    """
    known_pids = {
        str(r.get("pipeline_id") or "") for r in rows if isinstance(r, dict)
    } - {""}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parent_pid = str(row.get("pipeline_id") or "")
        if not parent_pid:
            continue
        # 活登记判定：任一 owned 字段值非空才算活登记（全 null 墓碑不重复写）
        ghost_ids: set[str] = set()
        for key, value in row.items():
            k = str(key)
            if not k.startswith("task.owned.") or value is None or value == "":
                continue
            owned_id = k[len("task.owned.") :].split(".", 1)[0]
            if owned_id and owned_id not in known_pids:
                ghost_ids.add(owned_id)
        if not ghost_ids:
            continue
        for owned_id in sorted(ghost_ids):
            fields: dict[str, Any] = {
                str(k): None
                for k in row
                if str(k).startswith(f"task.owned.{owned_id}.")
            }
            pending_key = f"task.subtasks_pending.{owned_id}"
            pending_val = row.get(pending_key)
            if pending_val is not None and pending_val != "":
                fields[pending_key] = None
            if not fields:
                continue
            swept = False
            try:
                await state_cap.call(
                    "update", {"pipeline_id": parent_pid, "fields": fields}
                )
                swept = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[task_reconcile] 幽灵 owned 登记清理写失败（降级留痕，下轮装载重试）"
                    "| parent=%s | owned=%s | err=%s",
                    parent_pid, owned_id, exc,
                )
            out.append(
                {
                    "kind": "ghost_owned_swept",
                    "parent_pipeline_id": parent_pid,
                    "owned_id": owned_id,
                    "fields_cleared": sorted(fields),
                    "swept": swept,
                }
            )
            logger.warning(
                "[task_reconcile] 幽灵 owned 登记清理（第六刀：子管道行不存在）"
                "| parent=%s | owned=%s | cleared=%s | swept=%s",
                parent_pid, owned_id, sorted(fields), swept,
            )
    return out


async def _migrate_legacy_timeout_rows(
    state_cap: Any, rows: list[Any]
) -> list[dict[str, Any]]:
    """第七刀：任务投影 timeout 存量行 → failed（ADR 2026-11-02-state-key-
    governance）。

    timeout 是无写点死枚举值（超时实际经 run 署名 → failed 写入，枚举在列
    却从未有写面），历史残留行迁移 failed 干净参与后续判定。不补
    task.ended_at（无事实时刻，不编造）。双守卫同迁移刀（任务行 + 血缘锚）；
    写失败仅告警留痕（下轮装载重试）。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not events.is_task_state(row):
            continue
        if not _is_task_pipeline_row(row):
            continue
        if str(row.get("task.status") or "") != "timeout":
            continue
        pid = str(row.get("pipeline_id") or "")
        task_id = str(row.get("task.id") or "")
        migrated = False
        try:
            await state_cap.call(
                "update",
                {"pipeline_id": pid, "fields": {"task.status": "failed"}},
            )
            migrated = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[task_reconcile] timeout 历史迁移写失败（降级留痕，下轮装载重试）"
                "| pipeline=%s | task=%s | err=%s",
                pid, task_id, exc,
            )
        else:
            logger.info(
                "[task_reconcile] timeout 历史迁移完成 | pipeline=%s | task=%s | "
                "timeout → failed",
                pid, task_id,
            )
        out.append(
            {
                "pipeline_id": pid,
                "task_id": task_id,
                "kind": "legacy_timeout_migrated",
                "legacy": "timeout",
                "migrated": migrated,
            }
        )
    return out


# ═══════════════════════════════════════════════════════════
# 第八刀：遗留 run.plugin_errors 超界行收敛
# （ADR 2026-11-17-bounded-error-ledger-and-iteration-cow P4 延伸）
# ═══════════════════════════════════════════════════════════

# 界值镜像引擎侧 kernel/crates/engine MAX_PLUGIN_ERROR_ENTRIES /
# MAX_PLUGIN_ERROR_MESSAGE_CHARS（跨语言共以此 ADR 为协调点，改动须同刀）。
_MAX_PLUGIN_ERROR_ENTRIES = 8
_MAX_PLUGIN_ERROR_MESSAGE_CHARS = 1024
_PLUGIN_ERROR_DEFAULT_CODE = "PLUGIN_EXEC_FAILED"


def _canonicalize_plugin_errors(entries: list[Any]) -> list[dict[str, Any]] | None:
    """遗留错误数组 → 引擎物化形态（``materialize_plugin_errors`` 同构）。

    Returns:
        界内（≤8 条且无超长 message）→ None（无需迁移，幂等省写）；
        超界 → 聚合收敛结果：按 ``(plugin_id, message)`` 聚合、首现序保留、
        末见 code 胜出、count = 全史出现次数、message 先截 1024 字符再聚合
        （同引擎记账口径）、截到前 8 组（引擎「第 9 种及以后丢弃」同界）。
        非 dict 条目不可解释，重构时丢弃。
    """
    dicts = [e for e in entries if isinstance(e, dict)]
    over_bound = len(entries) > _MAX_PLUGIN_ERROR_ENTRIES or any(
        len(str(e.get("message") or "")) > _MAX_PLUGIN_ERROR_MESSAGE_CHARS
        for e in dicts
    )
    if not over_bound:
        return None
    out: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for e in dicts:
        message = str(e.get("message") or "")[:_MAX_PLUGIN_ERROR_MESSAGE_CHARS]
        key = (str(e.get("plugin_id") or ""), message)
        code = str(e.get("code") or _PLUGIN_ERROR_DEFAULT_CODE)
        seen = index.get(key)
        if seen is None:
            entry: dict[str, Any] = {
                "plugin_id": key[0],
                "code": code,
                "message": message,
                "count": 1,
            }
            index[key] = entry
            if len(out) < _MAX_PLUGIN_ERROR_ENTRIES:
                out.append(entry)
        else:
            seen["count"] += 1
            seen["code"] = code
    return out


async def _migrate_legacy_plugin_error_rows(
    state_cap: Any, rows: list[Any]
) -> list[dict[str, Any]]:
    """第八刀：遗留 ``run.plugin_errors`` 超界行收敛到引擎物化形态。

    背景：有界错误账本（ADR 2026-11-17）只管新写——修复前旧内核逐次追加
    无界（装机实测单行 395KB / 单行 1152 条），存量行无任何收缩机制，前端
    state 快照每轮轮询原样拖出（实测 3.2MB/次）。本刀按键收敛，不设任务
    身份守卫：该键是引擎镜像键非任务域键，快照膨胀面覆盖会话管道（任务树
    与会话面板同源消费）。写失败降级留痕（观测面不阻断装载），下轮装载重试。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = state_fields.as_list(
            row.get("run.plugin_errors"), field="run.plugin_errors"
        )
        if not raw:
            continue
        canonical = _canonicalize_plugin_errors(raw)
        if canonical is None:
            continue
        pid = str(row.get("pipeline_id") or "")
        migrated = False
        try:
            await state_cap.call(
                "update",
                {"pipeline_id": pid, "fields": {"run.plugin_errors": canonical}},
            )
            migrated = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[task_reconcile] plugin_errors 历史迁移写失败（降级留痕，下轮装载重试）"
                "| pipeline=%s | before=%d | err=%s",
                pid, len(raw), exc,
            )
        else:
            logger.info(
                "[task_reconcile] plugin_errors 历史迁移完成 | pipeline=%s | "
                "%d 条 → %d 组（界内收敛）",
                pid, len(raw), len(canonical),
            )
        out.append(
            {
                "pipeline_id": pid,
                "kind": "legacy_plugin_errors_bounded",
                "before": len(raw),
                "after": len(canonical),
                "migrated": migrated,
            }
        )
    return out


# ═══════════════════════════════════════════════════════════
# 未回执补发面（ADR 2026-10-28-task-terminal-notify-loss-and-reopen-evidence D2）
# ═══════════════════════════════════════════════════════════


def _pending_registration_key(
    parent_row: dict[str, Any], tags: dict[str, Any], child_pipeline_id: str
) -> str | None:
    """父管道挂号键（值非空 = 该子任务的终态回执从未写过）。

    挂号键 = ``task.subtasks_pending.<子管道 id>``（task_submit 写入提交者
    管道的提交时间戳）。取值走两个等价形态：子管道 id 与任务 id——任务管道
    出生协议保证两者相等（task.id 恒等于 pipeline_id），规范形态取
    ``events.pending_registration_clear_fields``（在线终态路径清的就是它）。

    Returns:
        父管道行里**持有非空值**的挂号键；无（已回执 = null / 空串，或从未
        挂号 = 键缺席）→ None（本轮不补发）。
    """
    canonical = events.pending_registration_clear_fields(tags)
    candidates: list[str] = []
    for key in (
        f"task.subtasks_pending.{child_pipeline_id}" if child_pipeline_id else "",
        next(iter(canonical), "") if canonical else "",
    ):
        if key and key not in candidates:
            candidates.append(key)
    for key in candidates:
        value = parent_row.get(key)
        if value is None or value == "":
            continue
        return key
    return None


def _replay_window_hours() -> float | None:
    """未回执补发面的时效窗（小时）。None = 补发面关闭（显式 ≤ 0）。

    配置源 = 环境变量 ``AGENTOS_TASK_TERMINAL_REPLAY_WINDOW_HOURS``（默认 24h）。
    不可解析 → 回默认并留痕（配置坏掉不改变默认行为）；≤ 0 → 关闭补发面。
    """
    raw = str(os.environ.get(_REPLAY_WINDOW_ENV) or "").strip()
    if not raw:
        return _DEFAULT_REPLAY_WINDOW_HOURS
    try:
        hours = float(raw)
    except ValueError:
        logger.warning(
            "[task_reconcile] 补发时效窗配置不可解析（回默认 %sh）| env=%s | value=%s",
            _DEFAULT_REPLAY_WINDOW_HOURS, _REPLAY_WINDOW_ENV, raw,
        )
        return _DEFAULT_REPLAY_WINDOW_HOURS
    if hours <= 0:
        return None
    return hours


def _parse_epoch(value: Any) -> float | None:
    """ISO 时间戳 → epoch 秒；空/不可解析 → None。

    朴素时间戳（无时区）按 UTC 解读——仓内时间戳口径（``_now_iso``）带时区，
    无时区形态只出现在外部写入的历史数据。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _iso_from_epoch(epoch: float) -> str:
    """epoch 秒 → ISO 时间戳（UTC）——留痕用。"""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _terminal_moment(
    row: dict[str, Any], parent_row: dict[str, Any], pending_key: str
) -> tuple[float | None, str]:
    """候选终态时刻（epoch 秒）+ 取值来源标签。

    优先取子管道投影的 ``task.ended_at``（终态时刻；该键在 tasks 插件
    ``export_fields`` 出口内，读面可见）；缺失/不可解析时回退父管道登记时间
    （``task.subtasks_pending.<id>`` 的值 = 提交时间戳）——**取旧**（提交时刻
    早于终态时刻）→ 判定更保守 → 更少补发。

    Returns:
        ``(epoch, 来源标签)``；两处都不可判定 → ``(None, "unknown")``。
    """
    ended_at = _parse_epoch(row.get("task.ended_at"))
    if ended_at is not None:
        return ended_at, "task.ended_at"
    registered_at = _parse_epoch(parent_row.get(pending_key))
    if registered_at is not None:
        return registered_at, "parent_registration"
    return None, "unknown"


async def _write_pending_receipt(
    state_cap: Any, parent_pipeline_id: str, pending_key: str
) -> bool:
    """父管道挂号回执：挂号键写 null（无键删除语义，null 即已回执）。

    写失败仅告警留痕——下轮装载重试（重试 = 再补发一次，见模块 docstring 的
    幂等说明：幂等锚是「清挂号」，清失败则该 (父,子) 下一轮仍会命中）。
    """
    try:
        await state_cap.call(
            "update",
            {"pipeline_id": parent_pipeline_id, "fields": {pending_key: None}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task_reconcile] 父管道挂号回执写失败（降级留痕，下轮装载重试）"
            "| parent=%s | key=%s | err=%s",
            parent_pipeline_id, pending_key, exc,
        )
        return False
    logger.info(
        "[task_reconcile] 父管道子任务挂号已回执 | parent=%s | key=%s",
        parent_pipeline_id, pending_key,
    )
    return True


async def _replay_unreceipted_terminal_events(
    state_cap: Any, rows: list[dict[str, Any]], bus_cap: Any | None
) -> list[dict[str, Any]]:
    """未回执补发面：终态事件漏发（插件不在场窗口）→ 重发 + 回执 + 纠正父镜像。

    判据（两条同时成立才补发）：

    1. 子任务行：任务管道身份守卫同款（``_is_task_pipeline_row``：行含任一
       ``lineage.*`` 血缘键）+ ``events.is_task_state(row)``；其 ``task.status``
       ∈ {completed, failed}——**任务域自己的投影终态**（评估闸门 task_evaluate
       落的 task.status），**不是** runs 簿记这类第二真值（ADR 2026-10-20 已否
       「拿第二真值补写终态」）。本面不改写任何终态词汇，只重发已落定的终态事件。
    2. 父管道行（``lineage.parent_pipeline_id``）仍持有**非空**的
       ``task.subtasks_pending.<子管道 id>``——终态回执从未写过 → 那一轮的
       通知从未送达上级（域事件广播即发，无重放无死信，订阅者不在场即丢）。

    动作（与 ``events.handle_run_terminal_event`` 同构）：① 经 event-bus.emit_domain
    重发终态事件（标签组用 ``events.derive_task_terminal_events`` 的同一构造）；
    ② 父管道挂号键写 null（回执）。owned 镜像已退役
    （ADR 2026-11-02-state-key-governance）：无镜像纠正面。

    时效窗（ADR 2026-10-28 D2-1）：只补**上一轮工作窗口内漏发**的候选——终态
    时刻距今 ≤ 窗口（默认 24h，环境变量 ``AGENTOS_TASK_TERMINAL_REPLAY_WINDOW_HOURS``）
    才补发；超窗候选 = 历史积压（装机实测 9 条跨 9 天）→ **不通知也不清回执**
    （清了就永远没人知道它漏过），只留 WARNING 痕转人工治理。终态时刻取子管道
    投影 ``task.ended_at``，缺失时回退父登记时间（取旧 → 更保守）。窗口 ≤ 0 =
    显式关闭补发面（候选仍留痕）。

    降级与留痕：

    - event-bus 能力缺席（``bus_cap`` 为 None）→ 只做 ② 并 WARNING 留痕；
    - 事件重发抛错 → 记异常（不静默吞）并**保留挂号**（不写回执）：下轮装载
      重试，不把「未送达」变成「永久丢失」；
    - 写失败只告警留痕（下轮装载重试），不抛。

    Args:
        state_cap: pipeline-state 能力句柄（update 写面）。
        rows: pipeline-state.list 读面行（同一批快照，父管道行也在其中）。
        bus_cap: event-bus 能力句柄；None = 能力缺席（降级）。

    Returns:
        补发清单（每条含 parent/child/task/event 与各步落地标记）。
    """
    by_pipeline: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("pipeline_id") or "")
        if pid:
            by_pipeline[pid] = row

    # 时效窗与「现在」取一次（本轮口径统一；_time 为模块命名空间打桩点，
    # 时序测试经 setattr(reconcile, "_time", fake) 固定钟点）
    window_hours = _replay_window_hours()
    now = _time.time()

    # 扫描摘要（装机 2026-10-28 实测教训：判据任一环静默落空时只看到
    # terminal_replayed=0，分不清「没候选」与「读面缺键」——此处按环计数，
    # 一次装载一行 INFO，定位断点无需临时探针）
    scanned_rows = 0
    task_rows = 0
    terminal_rows = 0
    parent_missing = 0
    pending_missing = 0
    replayed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scanned_rows += 1
        # 任务管道身份守卫：行含任一 lineage.* 血缘键（会话管道恒无）
        if not _is_task_pipeline_row(row):
            continue
        task_rows += 1
        status = str(row.get("task.status") or "")
        run_event = _TERMINAL_REPLAY_RUN_EVENTS.get(status)
        if run_event is None:
            continue
        terminal_rows += 1
        # 任务域自身声明判据（events.is_task_state）由派生构造内部同一判据执行
        # ——派生不出即不是任务行（会话记账行天然落空）
        for name, tags in events.derive_task_terminal_events(run_event, row):
            parent = str(tags.get("parent_pipeline_id") or "")
            child_pipeline_id = str(tags.get("pipeline_id") or "")
            if not parent or not child_pipeline_id:
                continue
            parent_row = by_pipeline.get(parent)
            if parent_row is None:
                parent_missing += 1
                continue
            pending_key = _pending_registration_key(parent_row, tags, child_pipeline_id)
            if pending_key is None:
                pending_missing += 1
                continue

            task_id = str(tags.get("task_id") or "")
            # 时效窗判据（ADR 2026-10-28 D2-1）：只补「上一轮工作窗口内漏发」
            # 的候选；超窗 = 历史积压 → 不通知也不清回执，只留 WARNING 痕。
            moment, moment_source = _terminal_moment(row, parent_row, pending_key)
            moment_text = _iso_from_epoch(moment) if moment is not None else "unknown"
            if window_hours is None:
                logger.warning(
                    "[task_reconcile] 未回执终态候选跳过（补发面关闭：时效窗 ≤ 0）"
                    "| parent=%s | child=%s | task=%s | status=%s | terminal_at=%s",
                    parent, child_pipeline_id, task_id, status, moment_text,
                )
                continue
            if moment is None:
                logger.warning(
                    "[task_reconcile] 未回执终态候选跳过（终态时刻不可判定，不制造噪声）"
                    "| parent=%s | child=%s | task=%s | status=%s | source=%s",
                    parent, child_pipeline_id, task_id, status, moment_source,
                )
                continue
            age_seconds = now - moment
            if age_seconds > window_hours * 3600.0:
                logger.warning(
                    "[task_reconcile] 未回执终态候选超时效窗（历史积压，转人工治理："
                    "不通知、不清回执）| parent=%s | child=%s | task=%s | status=%s | "
                    "terminal_at=%s | source=%s | age_days=%.1f | window_hours=%s",
                    parent, child_pipeline_id, task_id, status, moment_text,
                    moment_source, age_seconds / 86400.0, window_hours,
                )
                continue

            entry: dict[str, Any] = {
                "kind": "unreceipted_terminal_replay",
                "pipeline_id": child_pipeline_id,
                "task_id": task_id,
                "parent_pipeline_id": parent,
                "event": name,
                "pending_key": pending_key,
                "terminal_at": moment_text,
                "window_hours": window_hours,
                "replayed": False,
                "receipt_written": False,
            }
            # ⓪ 血缘冒泡解析（ADR 2026-11-06 D3/D4）：与在线路径同一构造
            # （events.resolve_wakeup_target，复用本面的全行快照）——不接冒泡
            # 会把已终态的直接父任务重新唤醒（回归在线守卫要防的原缺陷）。
            # 直接父行缺席的候选已在上方跳过（读面降级保持现状），此处冒泡
            # 只会命中终点或结构性死路。
            target, dead_end, chain = events.resolve_wakeup_target(rows, parent)
            if dead_end:
                logger.warning(
                    "[task_reconcile] 未回执终态补发血缘冒泡结构性死路（链顶终态任务/"
                    "环/祖先行缺失——处置非漏发，清挂号防下轮重复触发）"
                    "| parent=%s | child=%s | task=%s | event=%s | chain=%s",
                    parent, child_pipeline_id, task_id, name, chain,
                )
                entry["dead_end"] = True
                entry["receipt_written"] = await _write_pending_receipt(
                    state_cap, parent, pending_key
                )
                replayed.append(entry)
                continue
            # tag 契约（字段只增不删）：wakeup_target_pipeline_id = 实际投递
            # 目标；parent_pipeline_id 恒=直接父不变（回执定位零改动）。
            tags["wakeup_target_pipeline_id"] = target
            # ① 重发终态事件（标签组 = 在线路径同一构造）
            if bus_cap is None:
                logger.warning(
                    "[task_reconcile] 未回执终态补发降级（event-bus 能力缺席，只回执）"
                    "| parent=%s | child=%s | task=%s | event=%s",
                    parent, child_pipeline_id, task_id, name,
                )
            else:
                try:
                    await bus_cap.call("emit_domain", {"event": name, "tags": tags})
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[task_reconcile] 未回执终态事件补发失败（挂号保留，下轮装载重试）"
                        "| parent=%s | child=%s | task=%s | event=%s",
                        parent, child_pipeline_id, task_id, name,
                    )
                else:
                    entry["replayed"] = True
                    logger.info(
                        "[task_reconcile] 未回执终态事件补发完成 | parent=%s | child=%s | "
                        "task=%s | event=%s",
                        parent, child_pipeline_id, task_id, name,
                    )
            # ② 父管道挂号回执（送达后清；能力缺席按降级口径照清；补发失败不写
            #    回执——挂号保留即下轮重试的锚）。owned 镜像已退役
            #    （ADR 2026-11-02-state-key-governance）：无 ③ 镜像纠正面。
            if bus_cap is None or entry["replayed"]:
                entry["receipt_written"] = await _write_pending_receipt(
                    state_cap, parent, pending_key
                )
            replayed.append(entry)
    logger.info(
        "[task_reconcile] 未回执补发面扫描摘要 | rows=%d | task=%d | terminal=%d | "
        "parent_missing=%d | pending_missing=%d | replayed=%d | window_hours=%s",
        scanned_rows, task_rows, terminal_rows, parent_missing, pending_missing,
        len(replayed), window_hours,
    )
    return replayed
