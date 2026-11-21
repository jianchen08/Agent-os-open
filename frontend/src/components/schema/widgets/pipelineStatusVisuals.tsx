/** 管道运行状态展示（文案/颜色/终态集），自 PipelineManagerWidget 拆出。 */
import {
  TASK_PANEL_DISPLAY_LABELS,
  taskPanelDisplayKey,
  type TaskPanelDisplayKey,
} from '@/types/taskStatus'
import type { PipelineStatus, PipelineViewEntry } from '@/types/pipeline'
import type { AgentTab } from '@/types/task'

/** 管道运行视图态 → Agent 标签页状态（未知落 'unknown'，不猜 running） */
export function pipelineStatusToTabStatus(status: PipelineStatus): AgentTab['status'] {
  switch (status) {
    case 'running':
      return 'running'
    case 'completed':
      return 'completed'
    case 'failed':
      return 'failed'
    case 'suspended':
    case 'cancelled':
      // 已暂停/已取消 = 停在那里等待用户
      return 'waiting_input'
    case 'unknown':
      return 'unknown'
  }
}

/** 管道运行状态 → 展示文案（对齐内核 RunStatus 五态；非任务条目仅此一态） */
export const PIPELINE_STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  suspended: '已暂停',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  unknown: '未知',
}

/** 状态 → 文字标签 + 颜色（状态在面板上只以文字标签呈现，无独立图标位） */
export function statusVisual(status: string): { color: string; label: string } {
  const map: Record<string, { color: string }> = {
    running: { color: 'text-status-info' },
    suspended: { color: 'text-status-pending' },
    completed: { color: 'text-status-success' },
    failed: { color: 'text-status-error' },
    cancelled: { color: 'text-muted-foreground' },
    unknown: { color: 'text-status-pending' },
  }
  const conf = map[status] ?? { color: 'text-status-pending' }
  return { color: conf.color, label: PIPELINE_STATUS_LABELS[status] ?? status }
}

/** 运行终态集（runs 权威，收束分组；suspended 可恢复不算终态） */
export const TERMINAL_PIPELINE_STATUSES: ReadonlySet<string> = new Set(['completed', 'failed', 'cancelled'])

/** 折算键 → 文字颜色（色板对齐 statusVisual：等待子任务=活跃等待归 info，
 *  已暂停=停在那里归 pending，与运行态 suspended 同色） */
const TASK_PANEL_DISPLAY_COLORS: Record<TaskPanelDisplayKey, string> = {
  running: 'text-status-info',
  waiting_subtasks: 'text-status-info',
  paused: 'text-status-pending',
  failed: 'text-status-error',
  completed: 'text-status-success',
  unknown: 'text-status-pending',
}

/**
 * 任务条目行状态视觉（设计 §4.5 显示聚合，一词一义）：
 * (task.status, 子任务挂号非空?) 折算出文案/颜色，不直出 run 域词汇
 * （「已取消」自任务面板退役，ADR 2026-10-30-task-status-authority-
 * vocabulary-convergence 决策 7）。既有防御不回退：
 * - BUG-2b 终态防御：折算为「运行中」而运行态已终（runs/state 权威）时不
 *   复活幽灵执行中——终态直出运行态视觉，其中 cancelled 终态按过渡映射落
 *   「已暂停（可恢复）」（已取消退役）；
 * - BUG-21 无证据防御：折算为「运行中」而运行态未知/缺席（runs/state 双面
 *   无行）时落「未知」，不猜 running。
 */
export function taskRowStatusVisual(
  taskStatus: string | undefined,
  hasPendingSubtasks: boolean,
  runStatus: string | undefined,
): { color: string; label: string } {
  const key = taskPanelDisplayKey(taskStatus, hasPendingSubtasks)
  if (key === 'running') {
    const runTerminal = runStatus != null && TERMINAL_PIPELINE_STATUSES.has(runStatus)
    if (runTerminal) {
      return runStatus === 'cancelled'
        ? { color: TASK_PANEL_DISPLAY_COLORS.paused, label: TASK_PANEL_DISPLAY_LABELS.paused }
        : statusVisual(runStatus)
    }
    if (runStatus == null || runStatus === 'unknown') {
      return { color: TASK_PANEL_DISPLAY_COLORS.unknown, label: TASK_PANEL_DISPLAY_LABELS.unknown }
    }
  }
  return { color: TASK_PANEL_DISPLAY_COLORS[key], label: TASK_PANEL_DISPLAY_LABELS[key] }
}
/** 条目运行态视觉（文案/颜色）单点：项目登记行无运行态返回 null；任务行按
 *  §4.5 一词一义折算（不直出 run 域词汇），会话行沿用 run 域词汇。行内标签
 *  与详情标题、面板树聚合三处共用本函数——同一状态的多处呈现不得各写一套。
 *  （自 PipelineManagerTreeParts 迁出：状态聚合需要同一判据，组件侧与数据面
 *  不得各判一套） */
export function entryStatusVisual(entry: PipelineViewEntry): { color: string; label: string } | null {
  if (entry.kind === 'project') return null
  if (entry.kind === 'task') {
    return taskRowStatusVisual(entry.taskStatus, entry.hasPendingSubtasks === true, entry.status)
  }
  return statusVisual(entry.status ?? '')
}

/** 条目自身是否已显示「运行中」：下辖运行中徽标只在自身未显示运行中时补位
 *  ——「我本人在跑」与「我的下级在跑」二义分离，不互相覆盖 */
export function entryShowsRunning(entry: PipelineViewEntry): boolean {
  return entryStatusVisual(entry)?.label === PIPELINE_STATUS_LABELS.running
}

/** 状态图标种类（行首「类型+状态合一」图标的取件键，单一真值）。
 *
 * 与 `entryStatusVisual` 出自**同一套分支判据**（taskRowStatusVisual 的统计口径），
 * 但取结构化键而非显示文案——文案措辞可变（如任务面板「已暂停（可恢复）」），
 * 以文案为键会让图标静默错位。项目登记行无运行态返回 null（渲染项目图标）。
 */
export type EntryStatusIconKind = 'running' | 'paused' | 'failed' | 'completed' | 'unknown'

export function entryStatusIconKind(entry: PipelineViewEntry): EntryStatusIconKind | null {
  if (entry.kind === 'project') return null
  if (entry.kind === 'task') {
    const key = taskPanelDisplayKey(entry.taskStatus, entry.hasPendingSubtasks === true)
    if (key === 'running') {
      // 终态防御（BUG-2b）：折算运行中而运行态已终 → 直出运行态口径
      const runTerminal = entry.status != null && TERMINAL_PIPELINE_STATUSES.has(entry.status)
      if (runTerminal) {
        if (entry.status === 'cancelled') return 'paused'
        return entry.status === 'failed' ? 'failed' : 'completed'
      }
      // 无证据防御（BUG-21）：运行态未知/缺席不猜 running
      if (entry.status == null || entry.status === 'unknown') return 'unknown'
      return 'running'
    }
    // 等待子任务 = 活跃等待，视觉归运行中
    if (key === 'waiting_subtasks') return 'running'
    return key
  }
  switch (entry.status) {
    case 'running':
      return 'running'
    case 'suspended':
    case 'cancelled':
      return 'paused'
    case 'failed':
      return 'failed'
    case 'completed':
      return 'completed'
    default:
      return 'unknown'
  }
}
