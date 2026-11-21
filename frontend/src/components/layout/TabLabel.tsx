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

import { useFadeOverflow } from '@/hooks/useFadeOverflow'
import { cn } from '@/lib/utils'

export interface TabLabelProps {
  /** 标签标题（完整文本；遮蔽只影响呈现，不截断数据） */
  title: string
  /** 额外类名（字号等由调用方决定） */
  className?: string
  /**
   * 收边变体（缺省 fade=渐变遮蔽，顶带两处标签既有契约不变）：
   * truncate = 省略号收边——移动面板形态（P1-2，MuMu 实测 shots/01）任务标签
   * 过长时用 truncate 给右侧图标组让位；渐变遮蔽在压缩态会把 CJK 字从中间
   * 切开（R9 巡检 #7 同款压字面），省略号在窄屏更可读。
   */
  variant?: 'fade' | 'truncate'
}

export function TabLabel({ title, className, variant = 'fade' }: TabLabelProps) {
  const { ref, overflowing } = useFadeOverflow()

  if (variant === 'truncate') {
    return (
      <span data-testid="tab-label" className={cn('min-w-0 truncate', className)}>
        {title}
      </span>
    )
  }

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
