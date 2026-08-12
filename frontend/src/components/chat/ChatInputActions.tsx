/**
 * ChatInputActions —— chat 空间 input-action 声明的工具栏渲染（架构 §5.4 chat SpaceHost 一环）
 *
 * 架构意图：插件声明的聊天输入动作（contributes.pages space=chat slot=input-action）
 * 在输入框工具栏显示为按钮，点击经 onAction 回调触发（回调方通常派发到 commandDispatcher）。
 *
 * 之前 chat 空间（getPagesBySpace('chat')）零消费者——本组件是 chat 空间声明驱动的
 * 第一个落点（消息卡片类 inline 声明属 TC S3 chat_card 协议，后续接入）。
 *
 * 关联：docs/working/重要设计/前端能力统一架构.md §5.3 / §5.4
 */

import { useCallback, useMemo } from 'react'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

export interface ChatInputActionsProps {
  /** 点击声明动作的回调（携带 PageDeclaration，调用方派发到 commandDispatcher 等） */
  onAction?: (page: PageDeclaration) => void
}

/**
 * 渲染 chat 空间 input-action 声明为工具栏按钮
 *
 * 无声明时返回 null，不渲染容器（不污染工具栏布局）。
 */
export function ChatInputActions({ onAction }: ChatInputActionsProps) {
  const actions = useMemo(
    () =>
      contributionRegistry
        .getPagesBySpace('chat')
        .filter((p) => p.slot === 'input-action')
        .sort((a, b) => (a.order ?? 50) - (b.order ?? 50)),
    [],
  )

  const handleClick = useCallback(
    (page: PageDeclaration) => {
      onAction?.(page)
    },
    [onAction],
  )

  if (actions.length === 0) return null

  return (
    <div className="flex items-center gap-1.5" data-testid="chat-input-actions">
      {actions.map((action) => (
        <button
          key={action.id}
          type="button"
          onClick={() => handleClick(action)}
          className="text-muted-foreground hover:text-foreground hover:bg-muted flex h-8 items-center gap-1 rounded-lg px-2 text-xs transition-colors"
          aria-label={action.title ?? action.id}
          title={action.title ?? action.id}
        >
          {action.icon && <span aria-hidden="true">{action.icon}</span>}
          {action.title && <span className="hidden sm:inline">{action.title}</span>}
        </button>
      ))}
    </div>
  )
}

export default ChatInputActions
