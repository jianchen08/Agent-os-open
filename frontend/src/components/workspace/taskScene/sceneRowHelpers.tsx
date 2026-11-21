/**
 * 现场行视觉辅助（纯函数/映射，无组件导出——与行组件分开以满足 fast-refresh
 * 只导出组件的要求）
 */

import {
  Box,
  FileText,
  Globe,
  LayoutGrid,
  MessageSquare,
  Terminal,
  Wrench,
} from '@/assets/icons'
import type { SceneToolCategory } from '@/utils/toolCategory'
import type { FC } from 'react'

/**
 * 类别 → 图标组件。
 *
 * 图标库（assets/icons，103 件）无 monitor / smartphone / app-window，按方案
 * §3.3.0 用现有近似件兜底（不新建图标库）：桌面→LayoutGrid、手机→MessageSquare、
 * 软件→Box、其它→Wrench。
 */
const CATEGORY_ICONS: Record<SceneToolCategory, FC<{ className?: string }>> = {
  terminal: Terminal,
  browser: Globe,
  desktop: LayoutGrid,
  phone: MessageSquare,
  software: Box,
  file: FileText,
  other: Wrench,
}

/** 类别 → 行图标（未收录类别落 Wrench，不抛错） */
export function sceneCategoryIcon(category: SceneToolCategory): FC<{ className?: string }> {
  return CATEGORY_ICONS[category] ?? Wrench
}

/** ISO 时间 → 本地 HH:mm:ss（非法值原样返回，不造假时间） */
export function formatStartTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}
