/**
 * 等待 AI 响应壳（ADR 2026-11-17-awaiting-reply-render-shell）。
 *
 * 直跑落定 → stream_start 窗口的派生观感件：user 气泡上屏同帧点亮，stream_start
 * 真气泡（ensureStreamingPlaceholder + startStreaming 同 tick）同一次 commit 替换
 * ——壳消失与真气泡出现零跳变。壳非消息实体（零乐观气泡=零残影不变量不破），
 * 幽灵风险由 store 三清口（startStreaming/stopStreaming/TTL）兜底。
 *
 * 外观对齐 MessageItem assistant 行（头像位 + --bubble-ai-* 令牌气泡盒），
 * 内容复用 StreamActivityIndicator（与真气泡首毫秒的「执行中」同源同款）——
 * 2026-11-19 用户裁定：全站活动态文案统一为「执行中」，不再区分思考/输出措辞。
 */
import { Bot } from 'lucide-react'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { cn } from '@/lib/utils'
import { StreamActivityIndicator } from './StreamActivityIndicator'

export function AwaitingReplyShell({
  since,
  className,
}: {
  /** user 消息落定时刻（Date.now()），计时锚点 */
  since: number
  className?: string
}) {
  return (
    <div
      className={cn(
        'group flex flex-col items-start gap-1 px-4 py-1.5 transition-colors',
        className,
      )}
      data-testid="awaiting-reply-shell"
      data-role="assistant"
    >
      <Avatar className="h-8 w-8 flex-shrink-0 rounded-xl bg-secondary text-secondary-foreground shadow-sm">
        <AvatarFallback className="rounded-xl text-sm font-medium">
          <Bot className="h-icon-md w-icon-md" />
        </AvatarFallback>
      </Avatar>
      <div className="flex min-w-0 w-full flex-col items-start">
        <div
          className="w-full overflow-hidden"
          // 主题气泡令牌可能是 linear-gradient() 形态，必须用 background 简写
          // 消费 var()（同 MessageItem bubbleStyle 先例），类名承载不了这组动态令牌
          // eslint-disable-next-line no-restricted-syntax
          style={{
            background: 'var(--bubble-ai-bg)',
            color: 'var(--bubble-ai-text)',
            borderRadius: 'var(--bubble-ai-radius, 1rem)',
            boxShadow: 'var(--bubble-ai-shadow)',
            border: 'var(--bubble-ai-border, none)',
            padding: 'var(--bubble-ai-padding, 0.75rem 1rem)',
          }}
        >
          <StreamActivityIndicator label="执行中" anchorMs={since} />
        </div>
      </div>
    </div>
  )
}
