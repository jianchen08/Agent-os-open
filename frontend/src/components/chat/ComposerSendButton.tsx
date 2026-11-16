/**
 * 发送/停止按钮 —— ChatInput 底部工具栏右组的三态按钮（shrink-0：不被左组挤出）。
 *
 * 三态：发送在途（已点发送、消息未落定）→ 转圈等待，气泡上屏（认领/队列条）
 * 前不回到可发送态，输入锁定期间无可键入新内容；执行中 → 停止生成（停止不依赖
 * 输入框内容，空输入时也能随时打断）；其余 → 发送（canSend 决定可用性）。
 */

import { Loader2, Send, Square } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export interface ComposerSendButtonProps {
  /** 发送已发出、消息尚未落定 */
  sendInFlight: boolean
  /** 生成/执行中（smart 模式看执行态，其余看生成态） */
  isExecuting: boolean
  /** 输入区有可发送内容 */
  hasInputContent: boolean
  /** 可发送（有内容且未禁用、无上传中附件） */
  canSend: boolean
  /** 停止生成（缺省时执行中仍显示发送态） */
  onStopGenerate?: () => void
  onSend: () => void
}

export function ComposerSendButton({
  sendInFlight,
  isExecuting,
  hasInputContent,
  canSend,
  onStopGenerate,
  onSend,
}: ComposerSendButtonProps) {
  return (
    <div className="flex shrink-0 items-center">
      {sendInFlight ? (
        <Button
          variant="default"
          size="icon"
          className="h-11 w-11 rounded-lg sm:h-8 sm:w-8"
          disabled
          title="消息发送中…"
          aria-label="消息发送中"
          data-testid="chat-send-inflight"
        >
          <Loader2 className="h-icon-md w-icon-md animate-spin" />
        </Button>
      ) : isExecuting && !hasInputContent && onStopGenerate ? (
        <Button
          variant="destructive"
          size="icon"
          className="h-11 w-11 rounded-lg sm:h-8 sm:w-8"
          onClick={onStopGenerate}
          title="停止生成"
          aria-label="停止生成"
          data-testid="chat-stop-button"
        >
          <Square className="h-icon-md w-icon-md" />
        </Button>
      ) : (
        <Button
          variant="default"
          size="icon"
          className={cn(
            'h-11 w-11 rounded-lg transition-all duration-200 sm:h-8 sm:w-8',
            canSend ? 'bg-primary hover:bg-primary/90 shadow-sm' : 'bg-muted text-muted-foreground',
          )}
          onClick={onSend}
          disabled={!canSend}
          title="发送消息"
          aria-label="发送消息"
          data-testid="chat-send-button"
        >
          <Send className="h-icon-md w-icon-md" />
        </Button>
      )}
    </div>
  )
}
