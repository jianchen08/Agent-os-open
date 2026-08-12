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
 * 状态码 → 中文文案映射（统一审查 §3.3 P3：消灭中英混排）
 *
 * StatusBadge 不传 label 时自动查表；未知状态回退原始值。
 */
const STATUS_LABELS: Record<string, string> = {
  active: '已启用',
  success: '成功',
  inactive: '未启用',
  pending: '待处理',
  error: '错误',
  failed: '失败',
  cancelled: '已取消',
  running: '运行中',
  waiting: '等待中',
  warning: '警告',
  disabled: '已停用',
  deprecated: '已弃用',
  info: '信息',
}

/**
 * 状态徽章组件
 *
 * 根据状态值自动映射到 Badge 变体，使用主题 CSS 变量控制颜色。
 */
export function StatusBadge({ status, label, size = 'sm' }: StatusBadgeProps) {
  const key = status.toLowerCase()
  const variant = STATUS_VARIANT_MAP[key] ?? 'default'
  const text = label ?? STATUS_LABELS[key] ?? status

  return (
    <Badge
      variant={variant}
      className={cn(size === 'md' && 'text-sm px-3 py-1')}
    >
      {text}
    </Badge>
  )
}
