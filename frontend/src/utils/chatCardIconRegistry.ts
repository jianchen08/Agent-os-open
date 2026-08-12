/**
 * chatCardIconRegistry —— chat_card.icon 字符串 → 图标组件（G4 / TC T2）
 *
 * 架构意图：插件用语义字符串声明卡片图标（ui.chat_card.icon: "terminal"），
 * 本表把字符串解析为 @/assets/icons 的图标组件，避免在声明里塞 JSX。
 * enhanceActivityWithToolConfig 用它把 interpreted.icon 设为 activity.customIcon。
 *
 * 关联：docs/working/design/tool-card-rendering-design.md §五（icon 字段）/ F2（iconRegistry）
 */

import {
  Copy,
  FileEdit,
  FileText,
  Globe,
  Link,
  Target,
  Terminal,
} from '@/assets/icons'
import type { LucideIcon } from '@/assets/icons'

/** 语义别名 → 图标组件（多别名指向同一图标，覆盖常见命名） */
const ICON_MAP: Record<string, LucideIcon> = {
  // 文件类
  file: FileText,
  file_read: FileText,
  read: FileText,
  edit: FileEdit,
  file_write: FileEdit,
  write: FileEdit,
  // 终端
  terminal: Terminal,
  bash: Terminal,
  shell: Terminal,
  // 网络
  globe: Globe,
  web: Globe,
  search: Globe,
  link: Link,
  fetch: Link,
  url: Link,
  // 任务/动作
  target: Target,
  task: Target,
  submit: Target,
  // 通用
  copy: Copy,
}

/** 默认图标（未命中时的兜底，避免空白） */
const DEFAULT_ICON: LucideIcon = Terminal

/**
 * 把 chat_card.icon 字符串解析为图标组件
 *
 * 大小写不敏感；未命中返回默认图标（不返回 null，保证 customIcon 总有值）。
 */
export function resolveChatCardIcon(name?: string): LucideIcon {
  if (!name) return DEFAULT_ICON
  return ICON_MAP[name.toLowerCase()] ?? DEFAULT_ICON
}

/** 注册自定义图标别名（插件运行期扩展用，当前无消费者，预留） */
export function registerChatCardIcon(name: string, icon: LucideIcon): void {
  ICON_MAP[name.toLowerCase()] = icon
}
