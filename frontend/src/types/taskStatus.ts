/**
 * 任务域状态词表单一真值源（总纲 #12）
 *
 * 权威词汇 = tasks 插件 TaskStatus 七态（plugins/shared/tasks/task_types.py
 * `class TaskStatus` / server.py task.list enum）。契约测试
 * types/__tests__/taskStatus.contract.test.ts 与插件写面字面量对账：
 * 后端增删状态、别名失去写面证据、或前端词表漂移时测试变红。
 */

import type { PipelineStatus } from './pipeline'
import type { AgentTabStatus } from './task'

/** 任务域权威七态（与 tasks 插件 TaskStatus enum 一一对应） */
export const TASK_STATUSES = [
  'pending',
  'running',
  'evaluating',
  'stopped',
  'completed',
  'failed',
  'timeout',
] as const

export type TaskStatus = (typeof TASK_STATUSES)[number]

/**
 * 旧值别名 → 七态归一（仅收录在后端源码中有读面/文档证据的旧持久化值；
 * stopped 合并旧 suspended/cancelled，见 task_types.py TaskStatus docstring）。
 * 键集合受契约测试锁死：无写面/读面证据的词（planning/blocked/deleted 等）不得回流。
 */
export const TASK_STATUS_ALIASES: Record<string, TaskStatus> = {
  suspended: 'stopped',
  paused: 'stopped',
  cancelled: 'stopped',
}

/**
 * 状态 → 中文展示文案。覆盖七态 + 别名（旧数据按原义展示）。
 * 展示文案单一出口，组件不得自备任务状态中文映射。
 */
export const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  running: '运行中',
  evaluating: '评估中',
  stopped: '已暂停',
  completed: '已完成',
  failed: '已失败',
  timeout: '已超时',
  // 别名旧值按折叠后语义展示（显示层一词一义）
  suspended: '已暂停',
  paused: '已暂停',
  // legacy：cancelled 已退役（ADR 2026-10-30-task-status-authority-vocabulary-
  // convergence），显示按折叠语义落「已暂停」；后端 reconcile 开机迁移
  // cancelled→stopped 完成后本别名条目可随 TASK_STATUS_ALIASES 一并删除
  cancelled: '已暂停',
}

/** 已告警过的未知状态值（同一未知值只警告一次） */
const warnedUnknownStatuses = new Set<string>()

/** 未知任务状态告警（模块内去重；normalize 与各投影共用） */
function warnUnknownStatus(value: string): void {
  if (!value || warnedUnknownStatuses.has(value)) return
  warnedUnknownStatuses.add(value)
  console.warn(`[taskStatus] 未知任务状态 "${value}"，按 unknown 处理`)
}

/**
 * 归一任务状态：别名折叠到七态；未知值返回 'unknown' 并 console.warn 一次
 * （未知 ≠ running/completed，禁止把未知猜成合法状态——诚实状态机口径）。
 */
export function normalizeTaskStatus(raw: unknown): TaskStatus | 'unknown' {
  const value = typeof raw === 'string' ? raw : ''
  if ((TASK_STATUSES as readonly string[]).includes(value)) {
    return value as TaskStatus
  }
  const aliased = TASK_STATUS_ALIASES[value]
  if (aliased) return aliased
  warnUnknownStatus(value)
  return 'unknown'
}

/** 任务状态 → 中文文案（未知值回退原串展示，不猜合法态） */
export function taskStatusLabel(raw: string): string {
  return TASK_STATUS_LABELS[raw] ?? raw
}

/**
 * 内核契约停机词（core::types::RunStatus::FAILED_STOP_REASONS + tasks/reconcile.py
 * 收录的两词第一方声明）→ 人话对照。与插件侧 _SIGNATURE_REASONS 同词表分面
 * 维护（面板渲染面不依赖 Python 侧）；词表外原词透传（插件声明停机词不猜语义）。
 * 导出仅供契约测试对账，组件一律经 stopReasonLabel 消费。
 */
export const STOP_REASON_LABELS: Record<string, string> = {  timeout: '墙钟预算耗尽',
  max_iterations: '轮次预算耗尽',
  budget_exhausted: '预算耗尽',
  elapsed_cap: '时长上限触发',
  iteration_cap: '轮次上限触发',
  stalled: '执行停滞',
  duplicate_loop: '重复循环',
  tool_fail_loop: '工具连续失败循环',
  dropped_tool_calls: '工具调用被丢弃',
  empty_reply_exhausted: '空回复耗尽（LLM 连续无内容回复）',
  task_failed: '任务判定失败',
  model_unavailable: '模型不可用',
  llm_fail_loop: 'LLM 连续同类失败循环',
}

/** 终局署名 → 展示文案：词表内「人话（原词）」，词表外原词透传 */
export function stopReasonLabel(raw: string): string {
  const described = STOP_REASON_LABELS[raw]
  return described ? `${described}（${raw}）` : raw
}

/**
 * run 域挂起归因前缀词表（P-23 片2，ADR 2026-11-04-run-suspended-reason-key）：
 * drain 排空写面的 suspended_reason → 人话对照。前缀匹配（reason 值带冒号
 * 后缀细节，如「memory watermark self-heal: private bytes 持续超阈值」）；
 * 词表外原词透传（drain 三触发源各传不同字串，不猜语义）。
 */
export const SUSPENDED_REASON_PREFIX_LABELS: ReadonlyArray<readonly [string, string]> = [
  ['memory watermark self-heal', '内存水位自愈'],
]

/** 挂起归因 → 展示文案：词表内「人话（原词）」，词表外原词透传 */
export function suspendedReasonLabel(raw: string): string {
  const hit = SUSPENDED_REASON_PREFIX_LABELS.find(([prefix]) => raw.startsWith(prefix))
  return hit ? `${hit[1]}（${raw}）` : raw
}

/**
 * 任务七态（+旧持久化值）→ 管道运行视图态。旧值按原义投影
 * （cancelled → 运行「已取消」非失败；suspended/paused → 「已暂停」）；
 * 未知值落 'unknown' 并告警一次，不猜 running。
 */
const TASK_TO_PIPELINE_STATUS: Record<string, PipelineStatus> = {
  pending: 'running',
  running: 'running',
  evaluating: 'running',
  stopped: 'suspended',
  completed: 'completed',
  failed: 'failed',
  timeout: 'failed',
  suspended: 'suspended',
  paused: 'suspended',
  cancelled: 'cancelled',
}

export function taskStatusToPipelineStatus(
  taskStatus: string | undefined,
): PipelineStatus | 'unknown' {
  if (!taskStatus) return 'unknown'
  const mapped = TASK_TO_PIPELINE_STATUS[taskStatus]
  if (mapped) return mapped
  warnUnknownStatus(taskStatus)
  return 'unknown'
}

/** 任务七态 → Agent 标签页视图态（未归一值先过 normalize，未知落 'unknown'） */
export function taskStatusToAgentTabStatus(raw: unknown): AgentTabStatus {
  switch (normalizeTaskStatus(raw)) {
    case 'completed':
      return 'completed'
    case 'failed':
    case 'timeout':
      return 'failed'
    case 'stopped':
      // 已暂停（可 continue 恢复）= 停在那里等待用户
      return 'waiting_input'
    case 'unknown':
      return 'unknown'
    default:
      return 'running'
  }
}

// ═════════════════════════════════════════════════════════════════
// 任务面板显示聚合（设计 §4.5 一词一义，ADR 2026-10-30-task-status-authority-
// vocabulary-convergence 决策 7）：面板任务行状态 = (task.status, 子任务挂号
// 非空?) 折算，不直出 run 域词汇文案。
// ═════════════════════════════════════════════════════════════════

/** 任务面板显示折算键（§4.5 五显示态 + unknown 诚实兜底） */
export type TaskPanelDisplayKey =
  | 'running'
  | 'waiting_subtasks'
  | 'paused'
  | 'failed'
  | 'completed'
  | 'unknown'

/** 折算键 → 面板展示文案（任务行状态标签单一出口，组件不得自备映射） */
export const TASK_PANEL_DISPLAY_LABELS: Record<TaskPanelDisplayKey, string> = {
  running: '运行中',
  waiting_subtasks: '等待子任务',
  paused: '已暂停（可恢复）',
  failed: '失败',
  completed: '已完成',
  unknown: '未知',
}

/** 任务结算判据集：子任务到达即写挂号回执（events.py 终态事件只对
 * completed/failed 派生）——「等待子任务」推导的已结算侧 */
const TASK_SETTLED_STATUSES: ReadonlySet<string> = new Set(['completed', 'failed'])

/**
 * (task.status, 子任务挂号非空?) → 面板显示键（§4.5 折算纯函数）。
 *
 * - running/evaluating → 运行中；pending 现有文案保留（面板现状经管道映射
 *   同显「运行中」）；timeout 同（现显「失败」）；
 * - stopped × 挂号非空 → 等待子任务；× 挂号空 → 已暂停（可恢复）。
 *   cancelled 为过渡期防御输入：经别名折叠落 stopped（legacy：后端 reconcile
 *   开机迁移 cancelled→stopped 完成后该输入不再出现，本函数无需改动）；
 * - failed/completed 直映；未知值落 unknown（不猜，诚实状态机口径）。
 */
export function taskPanelDisplayKey(
  rawTaskStatus: string | undefined,
  hasPendingSubtasks: boolean,
): TaskPanelDisplayKey {
  switch (normalizeTaskStatus(rawTaskStatus)) {
    case 'running':
    case 'evaluating':
    case 'pending': // 现有文案保留：面板现状 pending 显「运行中」
      return 'running'
    case 'stopped':
      return hasPendingSubtasks ? 'waiting_subtasks' : 'paused'
    case 'failed':
    case 'timeout': // 现有文案保留：面板现状 timeout 显「失败」
      return 'failed'
    case 'completed':
      return 'completed'
    case 'unknown':
      return 'unknown'
  }
}

/**
 * 子任务挂号推导（面板取数源声明）：管道 state 挂号键
 * ``task.subtasks_pending.*`` 不在内核 state 摘要白名单出口（面板数据拿不到
 * 该键），按任务列表现成父子关系推导——存在未结算直接子任务
 * （parent_task_id = 本任务 id 且状态不在结算集）⇔ 挂号非空（回执只在子任务
 * completed/failed 时写 null，与 TASK_SETTLED_STATUSES 同源）。
 * 返回「仍有未结算子任务」的父任务 id 集合。
 */
export function taskPendingSubtaskParents(
  tasks: readonly Record<string, unknown>[],
): Set<string> {
  const parents = new Set<string>()
  for (const task of tasks) {
    const id = String(task.id ?? '')
    const parent = String(task.parent_task_id ?? task.parentTaskId ?? '')
    // 自挂守卫（与面板建树同口径）：parent 指向自身不算子任务
    if (!parent || parent === id) continue
    const normalized = normalizeTaskStatus(
      typeof task.status === 'string' ? task.status : undefined,
    )
    if (!TASK_SETTLED_STATUSES.has(normalized)) parents.add(parent)
  }
  return parents
}
