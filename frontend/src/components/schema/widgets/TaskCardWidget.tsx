/**
 * 任务卡片组件
 *
 * 根据 ui_schema（type: "task_card"）渲染单条任务信息卡片，
 * 显示任务标题、状态、进度。适用于工作区任务列表、悬浮任务概览等场景。
 *
 * @module TaskCardWidget
 */

import React from 'react'
import { CheckCircle2, Clock, Loader2, AlertCircle, PauseCircle, XCircle } from 'lucide-react'

/** 任务状态（与全局 TaskStatus 对齐的子集，宽松匹配） */
type TaskCardStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'suspended'
  | 'cancelled'
  | 'evaluating'
  | 'scheduled'
  | 'timeout'

/** 状态 → 样式/图标/文案 */
const STATUS_META: Record<TaskCardStatus, { color: string; bg: string; icon: React.ReactNode; label: string }> = {
  pending: { color: 'text-status-pending', bg: 'bg-status-pending/10', icon: <Clock className="h-4 w-4" />, label: '待执行' },
  running: { color: 'text-status-info', bg: 'bg-status-info/10', icon: <Loader2 className="h-4 w-4 animate-spin" />, label: '执行中' },
  evaluating: { color: 'text-status-info', bg: 'bg-status-info/10', icon: <Loader2 className="h-4 w-4 animate-spin" />, label: '评估中' },
  scheduled: { color: 'text-status-pending', bg: 'bg-status-pending/10', icon: <Clock className="h-4 w-4" />, label: '已调度' },
  completed: { color: 'text-status-success', bg: 'bg-status-success/10', icon: <CheckCircle2 className="h-4 w-4" />, label: '已完成' },
  failed: { color: 'text-status-error', bg: 'bg-status-error/10', icon: <AlertCircle className="h-4 w-4" />, label: '已失败' },
  blocked: { color: 'text-status-warning', bg: 'bg-status-warning/10', icon: <AlertCircle className="h-4 w-4" />, label: '已阻塞' },
  suspended: { color: 'text-status-warning', bg: 'bg-status-warning/10', icon: <PauseCircle className="h-4 w-4" />, label: '已暂停' },
  cancelled: { color: 'text-muted-foreground', bg: 'bg-muted', icon: <XCircle className="h-4 w-4" />, label: '已取消' },
  timeout: { color: 'text-status-error', bg: 'bg-status-error/10', icon: <AlertCircle className="h-4 w-4" />, label: '已超时' },
}

/**
 * 规范化状态值
 *
 * 容忍大小写/未知值，统一映射到合法 TaskCardStatus（未知归入 pending）。
 *
 * @param raw - 原始状态值
 * @returns 规范化后的状态
 */
function normalizeStatus(raw: unknown): TaskCardStatus {
  if (typeof raw !== 'string') return 'pending'
  return (raw.toLowerCase() in STATUS_META)
    ? (raw.toLowerCase() as TaskCardStatus)
    : 'pending'
}

/**
 * 规范化进度值到 [0, 100]
 *
 * @param raw - 原始进度值
 * @returns 规范化后的进度
 */
function normalizeProgress(raw: unknown): number | undefined {
  if (typeof raw !== 'number' || Number.isNaN(raw)) return undefined
  return Math.max(0, Math.min(100, raw))
}

/**
 * 任务卡片组件
 *
 * @param props - 组件属性
 *   - title: 任务标题
 *   - status: 任务状态
 *   - progress: 进度值（0-100）
 *   - description: 任务描述
 *   - task_id: 任务 ID（用于跳转）
 * @returns 任务卡片渲染结果
 */
export function TaskCardWidget(props: Record<string, unknown>) {
  const title = (props.title as string) ?? '未命名任务'
  const status = normalizeStatus(props.status)
  const progress = normalizeProgress(props.progress)
  const description = props.description as string | undefined
  const taskId = props.task_id as string | undefined

  const meta = STATUS_META[status]

  return (
    <div className="rounded-lg border bg-background p-4 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <h4 className="text-foreground truncate text-sm font-semibold">{title}</h4>
          {taskId && (
            <span className="text-muted-foreground text-xs">{taskId}</span>
          )}
        </div>
        <span
          className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${meta.color} ${meta.bg}`}
        >
          {meta.icon}
          {meta.label}
        </span>
      </div>

      {description && (
        <p className="text-muted-foreground mt-2 line-clamp-2 text-xs">{description}</p>
      )}

      {progress !== undefined && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="text-muted-foreground">进度</span>
            <span className="text-foreground tabular-nums">{Math.round(progress)}%</span>
          </div>
          <div className="bg-muted h-1.5 w-full overflow-hidden rounded-full">
            <div
              className={`h-full rounded-full transition-all ${meta.color.replace('text-', 'bg-')}`}
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
