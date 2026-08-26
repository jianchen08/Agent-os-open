/**
 * 消息操作按钮组件
 *
 * 提供消息的复制与「重新生成」（最后一条 assistant 消息，含失败/中断态）操作。
 * 回退/编辑入口见批次 E（MessageActions 同文件演进）。
 * 所有操作入口均以回调形式暴露，调用方决定是否接入（不传回调即不显示按钮）。
 */

import { type FC } from 'react'
import { toast } from 'sonner'
import { Copy, RefreshCw } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import type { Message } from '@/types/models'

/**
 * 消息操作组件属性
 */
export interface MessageActionsProps {
  /** 消息对象 */
  message: Message
  /** 会话 ID */
  sessionId: string
  /** 是否为用户消息 */
  isUserMessage?: boolean
  /** 是否禁用操作 */
  disabled?: boolean
  /** 是否为最后一条消息（控制重试按钮显示） */
  isLastMessage?: boolean
  /** 消息复制回调 */
  onCopy?: () => void
  /** 消息编辑回调（批次 E 接入） */
  onEdit?: () => void
  /** 消息内容更新回调（用于版本切换） */
  onContentUpdate?: (content: string) => void
  /** 重新生成回调（最后一条 assistant 消息，含失败/中断态） */
  onRegenerate?: () => void
  /** 回退回调（user 消息二次确认后触发，批次 E 接入） */
  onRollbackTo?: (userMessageId: string) => void
}

/** 判定消息是否处于失败/中断态（空内容时仍需保留可重试入口） */
function isFailedOrInterrupted(message: Message): boolean {
  return (
    message.status === 'error' ||
    message.status === 'failed' ||
    message.status === 'interrupted'
  )
}

/**
 * 消息操作按钮组件
 */
export const MessageActions: FC<MessageActionsProps> = ({
  message,
  disabled = false,
  onCopy,
  onRegenerate,
}) => {
  /** 是否为最后一条 assistant 消息（重新生成入口：含失败/中断态） */
  const isAssistant = message.role === 'assistant'
  const showRegenerate =
    isAssistant && !!onRegenerate && (isFailedOrInterrupted(message) || message.status === 'completed')

  /**
   * 处理复制操作
   */
  const handleCopy = () => {
    if (onCopy) {
      onCopy()
    } else {
      navigator.clipboard.writeText(message.content)
      toast.success('已复制到剪贴板')
    }
  }

  return (
    <div className="flex items-center gap-0.5 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
      <Button
        variant="ghost"
        size="sm"
        className="h-6 w-6 p-0"
        onClick={handleCopy}
        disabled={disabled}
        title="复制"
      >
        <Copy className="h-icon-xs w-icon-xs" />
      </Button>
      {/* 重新生成：最后一条 assistant 消息（含失败/中断态） */}
      {showRegenerate && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={onRegenerate}
          disabled={disabled}
          title="重新生成"
          data-testid="regenerate-button"
        >
          <RefreshCw className="h-icon-xs w-icon-xs" />
        </Button>
      )}
    </div>
  )
}
