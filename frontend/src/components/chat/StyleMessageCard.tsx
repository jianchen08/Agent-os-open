/**
 * 插件消息卡容器（message_style 路由卡组件，自 MessageItem 绞杀者迁移）：
 * 非流式 assistant/system 携带 metadata.message_style 且声明命中 → 通用
 * webview 消息卡（PluginMessageCard）+ 角色头像。路由判定归
 * messageCardRouter，本组件只渲染。
 */
import { memo } from 'react'
import { PluginMessageCard } from './PluginMessageCard'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Bell, Bot } from '@/assets/icons'
import { cn } from '@/lib/utils'
import type { Message } from '@/types/models'

export const StyleMessageCard = memo(function StyleMessageCard({
  message,
  styleId,
  className = '',
}: {
  message: Message
  styleId: string
  className?: string
}) {
  const isSystemMessage = message.role === 'system'
  return (
    <div
      className={cn(
        'group hover:bg-muted/30 flex gap-3 px-4 py-2 transition-colors',
        'max-w-[calc(100%-44px)]',
        className,
      )}
      data-testid="message-item"
      data-role={message.role}
      data-message-style={styleId}
    >
      <Avatar
        className={cn(
          'h-8 w-8 flex-shrink-0 rounded-xl shadow-sm',
          isSystemMessage
            ? 'bg-status-warning/15 text-status-warning'
            : 'bg-secondary text-secondary-foreground',
        )}
      >
        <AvatarFallback className="rounded-xl text-sm font-medium">
          {isSystemMessage ? (
            <Bell className="h-icon-md w-icon-md" />
          ) : (
            <Bot className="h-icon-md w-icon-md" />
          )}
        </AvatarFallback>
      </Avatar>
      <div className="min-w-0 flex-1">
        <PluginMessageCard
          instanceKey={message.id}
          styleId={styleId}
          message={{ content: message.content, metadata: message.metadata }}
        />
      </div>
    </div>
  )
})
