/**
 * 现场悬浮标记角标（SceneBadge）
 *
 * 用途（方案 §3.5）：不能内嵌展示的内容**必须有可见标记**，让人知道「这里有内容
 * 可看」，而不是静默丢掉。典型场景：截图 base64 被截断需回取、二进制/未知类型
 * 产物只能打开页签看、大文件需分页加载。
 *
 * 样式复用现有徽标语言（`ModePanelBadge` 的圆角/字号档 + `text-status-*` 语义色，
 * 全部取既有 design token），不新造徽标组件族（方案 §九-14 被否项）。
 */

import { cn } from '@/lib/utils'
import type { FC } from 'react'

/** 角标语气（语义色，取既有 status token） */
export type SceneBadgeTone = 'info' | 'warning' | 'muted'

const TONE_CLASS: Record<SceneBadgeTone, string> = {
  info: 'text-status-info border-status-info/40 bg-status-info/10',
  warning: 'text-status-waiting border-status-waiting/40 bg-status-waiting/10',
  muted: 'text-muted-foreground border-border bg-muted/40',
}

export interface SceneBadgeProps {
  /** 角标文案（短：图 / 大文件 / 可查看 / 原始记录） */
  label: string
  /** 语气，缺省 info */
  tone?: SceneBadgeTone
  /** 悬停说明（补全「点开能看到什么」） */
  title?: string
  className?: string
}

/** 内容可看提示角标 */
export const SceneBadge: FC<SceneBadgeProps> = ({ label, tone = 'info', title, className }) => (
  <span
    className={cn(
      'inline-flex flex-shrink-0 items-center rounded border px-1 text-[10px] leading-4 whitespace-nowrap',
      TONE_CLASS[tone],
      className,
    )}
    title={title ?? label}
    data-testid="scene-badge"
  >
    {label}
  </span>
)
