/** 管道运行状态展示（文案/图标/终态集），自 PipelineManagerWidget 拆出。 */
import { Ban, CheckCircle2, CircleDot, Loader2, PauseCircle, XCircle } from 'lucide-react'
import React from 'react'
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

/** 状态 → 图标 + 颜色 */
export function statusIcon(status: string): { icon: React.ReactNode; color: string; label: string } {
  const map: Record<string, { icon: React.ReactNode; color: string }> = {
    running: { icon: <Loader2 className="h-4 w-4 animate-spin" />, color: 'text-status-info' },
    suspended: { icon: <PauseCircle className="h-4 w-4" />, color: 'text-status-pending' },
    completed: { icon: <CheckCircle2 className="h-4 w-4" />, color: 'text-status-success' },
    failed: { icon: <XCircle className="h-4 w-4" />, color: 'text-status-error' },
    cancelled: { icon: <Ban className="h-4 w-4" />, color: 'text-muted-foreground' },
    unknown: { icon: <CircleDot className="h-4 w-4" />, color: 'text-status-pending' },
  }
  const conf = map[status] ?? { icon: <CircleDot className="h-4 w-4" />, color: 'text-status-pending' }
  return { icon: conf.icon, color: conf.color, label: PIPELINE_STATUS_LABELS[status] ?? status }
}

/** 运行终态集（runs 权威，收束分组；suspended 可恢复不算终态） */
export const TERMINAL_PIPELINE_STATUSES: ReadonlySet<string> = new Set(['completed', 'failed', 'cancelled'])
