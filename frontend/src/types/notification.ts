/**
 * 通知系统类型定义
 *
 * 支持非阻塞通知、时间倒序展示、阻塞式升级
 */

/** 通知优先级 */
export type NotificationPriority = 'low' | 'normal' | 'high' | 'critical'

/** 通知分类 */
export type NotificationCategory = 'progress' | 'alert' | 'info' | 'success' | 'error'

/** 通知动作按钮 */
export interface NotificationAction {
  /** 动作 ID */
  id: string
  /** 按钮文本 */
  label: string
  /** 按钮样式变体 */
  variant?: 'default' | 'outline' | 'destructive' | 'ghost'
  /** 点击后的行为 */
  action: 'dismiss' | 'confirm' | 'navigate' | 'custom'
  /** 自定义动作的载荷 */
  payload?: Record<string, unknown>
}

/** 单条通知 */
export interface NotificationItem {
  /** 通知唯一标识 */
  id: string
  /** 通知类型 */
  category: NotificationCategory
  /** 标题 */
  title: string
  /** 消息内容（支持 Markdown） */
  message?: string
  /** 优先级 */
  priority: NotificationPriority
  /** 进度百分比 (0-100)，仅 progress 类型 */
  progress?: number
  /** 是否为阻塞式通知 */
  isBlocking: boolean
  /** 是否已读 */
  isRead: boolean
  /** 创建时间（ISO 8601） */
  timestamp: string
  /** 关联的 Agent ID */
  agentId?: string
  /** 关联的任务 ID */
  taskId?: string
  /** 关联的会话 ID */
  sessionId?: string
  /** 关联的 Tab ID */
  tabId?: string
  /** 动作按钮列表 */
  actions?: NotificationAction[]
  /** 自动消失时间（毫秒），0 = 不自动消失 */
  autoDismissMs?: number
  /**
   * 合并键（2026-11-18 装机事故批次 3）：同键重复投递更新既有条目而非新增
   * （77 次重连这类高频失败合并为单条更新，不刷屏）。用户手动关闭后键随条目
   * 消失，下一次新失败以新 id 重新入列（不被 dismissed 记忆永久静默）。
   */
  mergeKey?: string
  /** 来源标识（如 pipeline_id，用于关联流式事件） */
  sourceId?: string
  /** 错误来源（统一错误模型 config/kernel/error_codes.json sources.enum，渲染来源标签） */
  errorSource?: 'kernel' | 'plugin' | 'llm' | 'infra' | 'frontend'
  /** 人类可读来源（如 Agent 名称/「前端」），渲染来源标签 */
  sourceLabel?: string
}

/** 优先级对应的颜色和图标映射（条目自身是优先级的唯一视觉出口） */
export const PRIORITY_STYLES: Record<
  NotificationPriority,
  { bg: string; border: string; text: string; icon: string; pulse: boolean }
> = {
  critical: {
    bg: 'bg-status-error/10',
    border: 'border-status-error/60',
    text: 'text-status-error',
    icon: 'AlertTriangle',
    pulse: true,
  },
  high: {
    bg: 'bg-status-warning/10',
    border: 'border-status-warning/50',
    text: 'text-status-warning',
    icon: 'AlertCircle',
    pulse: false,
  },
  normal: {
    bg: 'bg-status-info/5',
    border: 'border-status-info/30',
    text: 'text-status-info',
    icon: 'Info',
    pulse: false,
  },
  low: {
    bg: 'bg-status-pending/5',
    border: 'border-status-pending/20',
    text: 'text-status-pending',
    icon: 'Bell',
    pulse: false,
  },
}
