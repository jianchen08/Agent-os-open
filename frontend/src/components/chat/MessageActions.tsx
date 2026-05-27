/**
 * 消息操作按钮组件
 *
 * 提供消息的复制功能
 */

import { Copy } from 'lucide-react'
import { type FC } from 'react'
import { toast } from 'sonner'
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
  /** 消息编辑回调 */
  onEdit?: () => void
  /** 消息内容更新回调（用于版本切换） */
  onContentUpdate?: (content: string) => void
}

/**
 * 消息操作按钮组件
 */
export const MessageActions: FC<MessageActionsProps> = ({
  message,
  disabled = false,
  onCopy,
}) => {
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
        <Copy className="h-3 w-3" />
      </Button>
    </div>
  )
}
