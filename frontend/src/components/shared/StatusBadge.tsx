/**
 * StatusBadge 状态徽章
 *
 * 统一状态样式映射，替代各页面中重复的 getStatusStyle() 函数。
 * 使用主题系统的 badge CSS 变量（--badge-*-bg/text/border），
 * 通过 shadcn/ui Badge 组件确保与主题一致。
 */

import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

/** Badge 变体类型（与 badge.tsx 保持同步） */
type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'info' | 'outline'

/** StatusBadge 组件属性 */
interface StatusBadgeProps {
  /** 状态字符串，如 'active'、'error'、'running' 等 */
  status: string
  /** 可选的显示文字，不传则直接显示 status */
  label?: string
  /** 徽章尺寸，默认 'sm' */
  size?: 'sm' | 'md'
}

/**
 * 状态到 Badge 变体的映射
 *
 * 支持的状态别名：
 * - success 类: active, success → badge success 变体
 * - pending 类: inactive, pending → badge default 变体
 * - error 类: error, failed, cancelled → badge destructive 变体
 * - running 类: running → badge info 变体
 * - warning 类: waiting, warning → badge warning 变体
 * - disabled 类: disabled, deprecated → badge secondary 变体
 * - info 类: info → badge info 变体
 */
const STATUS_VARIANT_MAP: Record<string, BadgeVariant> = {
  // success 类
  active: 'success',
  success: 'success',
  // pending 类
  inactive: 'secondary',
  pending: 'default',
  // error 类
  error: 'destructive',
  failed: 'destructive',
  cancelled: 'destructive',
  // running 类
  running: 'info',
  // warning 类
  waiting: 'warning',
  warning: 'warning',
  // disabled 类
  disabled: 'secondary',
  deprecated: 'secondary',
  // info 类
  info: 'info',
}

/**
 * 获取状态对应的 Tailwind 颜色类名
 *
 * 使用主题 badge CSS 变量，替代不生效的 bg-status-xxx/10 模式。
 * 可在 StatusBadge 之外单独使用，用于需要自定义渲染但复用颜色映射的场景。
 *
 * @param status - 状态字符串
 * @returns Tailwind 类名字符串
 */
export function getStatusColorClass(status: string): string {
  const variant = STATUS_VARIANT_MAP[status.toLowerCase()] ?? 'default'
  const map: Record<string, string> = {
    default: 'bg-[var(--badge-default-bg)] text-[var(--badge-default-text)] border-[var(--badge-default-border)]',
    secondary: 'bg-[var(--badge-secondary-bg)] text-[var(--badge-secondary-text)] border-[var(--badge-secondary-border)]',
    destructive: 'bg-[var(--badge-error-bg)] text-[var(--badge-error-text)] border-[var(--badge-error-border)]',
    success: 'bg-[var(--badge-success-bg)] text-[var(--badge-success-text)] border-[var(--badge-success-border)]',
    warning: 'bg-[var(--badge-warning-bg)] text-[var(--badge-warning-text)] border-[var(--badge-warning-border)]',
    info: 'bg-[var(--badge-info-bg)] text-[var(--badge-info-text)] border-[var(--badge-info-border)]',
    outline: 'text-foreground border-current',
  }
  return map[variant] ?? map.default
}

/**
 * 状态徽章组件
 *
 * 根据状态值自动映射到 Badge 变体，使用主题 CSS 变量控制颜色。
 */
export function StatusBadge({ status, label, size = 'sm' }: StatusBadgeProps) {
  const variant = STATUS_VARIANT_MAP[status.toLowerCase()] ?? 'default'

  return (
    <Badge
      variant={variant}
      className={cn(size === 'md' && 'text-sm px-3 py-1')}
    >
      {label ?? status}
    </Badge>
  )
}
