/**
 * 消息卡路由（消息流渲染器化 · 分发器核心，ADR 2026-10-03）：
 * 消息 → 卡片声明的路由决策单点。宿主消息行只剩「问路由 → 渲染卡 / fallback
 * 骨架」，新消息卡形态 = 路由加一个 kind + 一个卡组件，不再往 MessageItem
 * 塞提前 return 分支。
 *
 * 路由优先级（与既有渲染顺序逐字对齐，特征化护栏锁定）：
 * 1. tool-card：role=tool（无其他门——工具结果消息恒走活动卡）
 * 2. style-card：非流式 assistant/system 携带 metadata.message_style 且
 *    registry 有声明（插件禁用/未声明同源消失 → fallback；流式期间正文仍在
 *    到达，卡片属终态渲染，完成后接管）
 */
import { MESSAGE_STYLE_METADATA_KEY, resolveMessageStyle } from './PluginMessageCard'
import type { Message } from '@/types/models'

export type MessageCardRoute =
  | { kind: 'tool-card' }
  | { kind: 'style-card'; styleId: string }
  | null

export function resolveMessageCardRoute(message: Message): MessageCardRoute {
  if (message.role === 'tool') {
    return { kind: 'tool-card' }
  }
  if (
    (message.role === 'assistant' || message.role === 'system') &&
    message.status !== 'streaming'
  ) {
    const styleId = message.metadata?.[MESSAGE_STYLE_METADATA_KEY]
    if (typeof styleId === 'string' && resolveMessageStyle(styleId)) {
      return { kind: 'style-card', styleId }
    }
  }
  return null
}
