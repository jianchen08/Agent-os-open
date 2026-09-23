/**
 * TabLabel —— 顶带标签文字（渐变遮蔽收边，两处标签共用）
 *
 * 顶带标签契约（用户裁定 2026-09-21）：
 * - 默认：标签保持宽度上限（max-w-[200px]），长标题**不用省略号**，
 *   而是在尾部用渐变把文字淡出（硬裁切 + mask 渐变收边）；
 * - 被挤压：标签随容器收缩到下限（min-w-[64px]），文字少显示一部分，
 *   遮蔽宽度按可见宽度收敛，仍保留「尾部还有内容」的视觉提示。
 *
 * 收在单组件里，对话标签（AgentTabItem）与工作区标签（WorkspacePanel）共用，
 * 避免两处各写一遍遮蔽逻辑（复制必漂移）。遮蔽样式由 useFadeOverflow 直写到
 * 本元素（见该 hook 注释：不走 React style 对象）。
 */

import { cn } from '@/lib/utils'
import { useFadeOverflow } from '@/hooks/useFadeOverflow'

export interface TabLabelProps {
  /** 标签标题（完整文本；遮蔽只影响呈现，不截断数据） */
  title: string
  /** 额外类名（字号等由调用方决定） */
  className?: string
}

export function TabLabel({ title, className }: TabLabelProps) {
  const { ref, overflowing } = useFadeOverflow()

  return (
    <span
      ref={ref}
      data-testid="tab-label"
      data-overflowing={overflowing ? 'true' : 'false'}
      className={cn('min-w-0 overflow-hidden whitespace-nowrap', className)}
    >
      {title}
    </span>
  )
}
