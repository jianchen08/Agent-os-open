/**
 * 任务域状态词表单一真值源（总纲 #12）
 *
 * 权威词汇 = tasks 插件 TaskStatus 七态（plugins/shared/system/tasks/task_types.py
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
 * 状态 → 中文展示文案。覆盖七态 + 别名（旧数据按原义展示）+
 * pending_evaluation（评估闸门未决值，读面见 tasks/reconcile.py 未决集）。
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
  // 别名旧值保留原义展示
  suspended: '已暂停',
  paused: '已暂停',
  cancelled: '已取消',
  // 评估未决（读面值）
  pending_evaluation: '待评估',
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
