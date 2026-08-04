/**
 * 活动类型定义
 * 统一工具调用、任务卡片等所有活动的数据结构
 *
 * @module activity
 */

import type { ReactNode } from 'react'

/**
 * 活动类型枚举
 */
export type ActivityType =
  | 'tool_call' // 工具调用
  | 'task_created' // 任务创建
  | 'task_phase' // 任务阶段
  | 'task_completed' // 任务完成
  | 'task_failed' // 任务失败
  | 'agent_thinking' // 思考过程
  | 'custom' // 自定义类型（可扩展）

/**
 * 活动状态枚举
 */
export type ActivityStatus =
  | 'pending' // 等待中
  | 'running' // 执行中
  | 'completed' // 已完成
  | 'failed' // 失败
  | 'cancelled' // 已取消

/**
 * 详情区块内容类型
 */
export type DetailContentType =
  | 'text' // 纯文本
  | 'json' // JSON 数据
  | 'code' // 代码块
  | 'markdown' // Markdown
  | 'diff' // 差异对比（oldContent/newContent 渲染为统一 diff 视图）

/**
 * 活动操作类型
 */
export type ActivityActionType =
  | 'retry' // 重试
  | 'delete' // 删除
  | 'cancel' // 取消
  | 'copy' // 复制
  | 'custom' // 自定义操作

/**
 * 详情区块接口
 */
export interface ActivityDetailBlock {
  /** 区块ID（可选，用于唯一标识） */
  id?: string
  /** 区块标题 */
  label: string
  /** 区块内容 */
  content: string | Record<string, unknown>
  /** 内容类型 */
  contentType?: DetailContentType
  /** 编程语言（仅 contentType='code' 时有效） */
  language?: string
  /** 是否可折叠 */
  collapsible?: boolean
  /** 默认是否展开 */
  defaultExpanded?: boolean
  /** 差异对比旧内容（仅 contentType='diff' 时有效） */
  diffOld?: string
  /** 差异对比新内容（仅 contentType='diff' 时有效） */
  diffNew?: string
}

/**
 * 活动操作接口
 */
export interface ActivityAction {
  /** 操作ID */
  id: string
  /** 操作图标 */
  icon: ReactNode
  /** 操作标签 */
  label: string
  /** 操作类型 */
  type: ActivityActionType
  /** 是否禁用 */
  disabled?: boolean
  /** 点击处理函数 */
  onClick: () => void | Promise<void>
  /** 确认提示（可选） */
  confirmMessage?: string
  /** 操作按钮样式（可选） */
  variant?: 'default' | 'destructive' | 'ghost' | 'outline' | 'secondary' | 'link'
}

/**
 * 活动数据主接口
 */
export interface ActivityData {
  /** 活动类型 */
  type: ActivityType
  /** 活动ID（唯一标识） */
  id: string
  /** 活动标题/名称 */
  title: string
  /** 工具名称（仅 type=tool_call 时有值，用于工具级渲染配置匹配） */
  toolName?: string
  /** 关联的文件路径（如 file_read/file_write 操作的文件） */
  filePath?: string
  /** 打开文件回调（点击文件名时调用） */
  onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
  /** 活动状态 */
  status: ActivityStatus
  /** 状态文本描述（可选，有默认值） */
  statusText?: string
  /** 执行时长（毫秒） */
  durationMs?: number
  /** 进度百分比 (0-100) */
  progress?: number
  /** 当前执行步骤描述 */
  currentStep?: string
  /** 预计剩余时间(毫秒) */
  estimatedRemainingMs?: number
  /** 中间输出列表（流式追加） */
  partialOutput?: string[]
  /** 详情区块列表 */
  details?: ActivityDetailBlock[]
  /** 头部展示的增删行数徽标（如 file_write 的 +X -Y） */
  diffStat?: { added: number; removed: number }
  /** 错误信息 */
  error?: string
  /** 时间戳 */
  timestamp?: string
  /** 可操作项（重试、删除等） */
  actions?: ActivityAction[]
  /** 自定义图标（可选） */
  customIcon?: ReactNode
  /** 自定义颜色（可选） */
  customColor?: string
  /** 自定义样式类名（可选） */
  customClassName?: string
}

/**
 * ActivityCard 组件属性接口
 */
export interface ActivityCardProps {
  /** 活动数据 */
  activity: ActivityData
  /** 是否默认展开 */
  defaultExpanded?: boolean
  /** 头部点击事件 */
  onHeaderClick?: () => void
  /** 自定义类名 */
  className?: string
  /** 自定义样式 */
  style?: React.CSSProperties
}

/**
 * 状态文本映射
 */
export const STATUS_TEXT_MAP: Record<ActivityStatus, string> = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/**
 * 活动类型文本映射
 */
export const ACTIVITY_TYPE_TEXT_MAP: Record<ActivityType, string> = {
  tool_call: '工具调用',
  task_created: '任务创建',
  task_phase: '任务阶段',
  task_completed: '任务完成',
  task_failed: '任务失败',
  agent_thinking: '思考过程',
  custom: '自定义',
}

/**
 * 获取状态颜色类名
 */
export function getStatusColorClass(status: ActivityStatus): string {
  const colorMap: Record<ActivityStatus, string> = {
    pending: 'text-status-warning',
    running: 'text-status-info',
    completed: 'text-status-success',
    failed: 'text-status-error',
    cancelled: 'text-status-pending',
  }
  return colorMap[status] || colorMap.pending
}

/**
 * 获取状态背景色类名
 */
export function getStatusBgColorClass(status: ActivityStatus): string {
  const colorMap: Record<ActivityStatus, string> = {
    pending: 'bg-status-warning/10',
    running: 'bg-status-info/10',
    completed: 'bg-status-success/10',
    failed: 'bg-status-error/10',
    cancelled: 'bg-status-pending/10',
  }
  return colorMap[status] || colorMap.pending
}

/**
 * 格式化时长
 */
export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`
  }
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) {
    return `${seconds}s`
  }
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds % 60
  return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`
}

/**
 * 格式化时间戳
 */
export function formatTimestamp(timestamp: string): string {
  const date = new Date(timestamp)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSecs = Math.floor(diffMs / 1000)
  const diffMins = Math.floor(diffSecs / 60)
  const diffHours = Math.floor(diffMins / 60)
  const diffDays = Math.floor(diffHours / 24)

  if (diffSecs < 60) {
    return '刚刚'
  }
  if (diffMins < 60) {
    return `${diffMins}分钟前`
  }
  if (diffHours < 24) {
    return `${diffHours}小时前`
  }
  if (diffDays < 7) {
    return `${diffDays}天前`
  }

  return date.toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
  })
}

/**
 * 获取状态文本
 */
export function getStatusText(status: ActivityStatus): string {
  return STATUS_TEXT_MAP[status] || '未知'
}
