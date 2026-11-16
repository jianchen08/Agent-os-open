/** 管道运行状态展示（文案/颜色/终态集），自 PipelineManagerWidget 拆出。 */
import {
  TASK_PANEL_DISPLAY_LABELS,
  taskPanelDisplayKey,
  type TaskPanelDisplayKey,
} from '@/types/taskStatus'
import type { PipelineStatus } from '@/types/pipeline'
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
