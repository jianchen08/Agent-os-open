/**
 * EmptyState 空状态
 *
 * 居中展示图标 + 标题 + 说明文字 + 可选操作按钮。
 * 参考现有 AgentsPage、AdminPage 的空状态实现，提取为统一组件。
 */

import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

/** EmptyState 组件属性 */
interface EmptyStateProps {
  /** 图标组件，传入 lucide-react 图标 */
  icon: LucideIcon
  /** 标题文字 */
  title: string
  /** 说明文字 */
  description?: string
  /** 可选的操作区，如添加按钮 */
  action?: ReactNode
}

/**
 * 空状态组件
 *
 * 居中展示大图标 + 标题 + 描述 + 可选操作。
 * 遵循项目中已有的空状态视觉模式。
 */
export function EmptyState({ icon: Icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16">
      <Icon className="text-muted-foreground/40 mb-3 h-12 w-12" />
      <p className="text-muted-foreground text-sm">{title}</p>
      {description && (
        <p className="text-muted-foreground/60 mt-1 text-xs">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}
