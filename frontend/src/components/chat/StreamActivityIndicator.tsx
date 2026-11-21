/**
 * 统一流式活性指示（2026-11-06 用户裁定）：思考中/执行中/压缩中的转圈图标
 * 统一为单一形态（StreamSpinner），状态行后跟实时总耗时（StreamElapsedText）。
 * 计时锚点 = 触发本次执行的消息（用户消息或通知/触发注入消息）发出时刻——
 * 从消息发出计到整条消息结束；圈随消息终态消失，计时同葬（执行态不在即无
 * 总时可计）。
 */
import { useEffect, useState } from 'react'
import { Loader2 } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { formatDuration } from '@/types/activity'
import type { ReactNode } from 'react'

/** 计时刷新粒度：0.1s——一位小数秒的小数位按此节奏跳动，满 1s 秒位才进
 *  （2026-11-07 用户纠偏：有小数点就该小数位动，1s 粒度下小数位恒静止） */
const TICK_MS = 100

/** 统一转圈图标（思考中/执行中/压缩中共用形态） */
export function StreamSpinner({ className }: { className?: string }): ReactNode {
  return <Loader2 className={cn('h-icon-md w-icon-md animate-spin text-status-info', className)} />
}

/** 秒级自跳时钟（enabled 才起 interval） */
function useStreamNow(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    const timer = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(timer)
  }, [enabled])
  return now
}

/** 实时总耗时文本：锚点缺席不渲染（历史消息/无前置 user 消息静默降级） */
export function StreamElapsedText({
  anchorMs,
  className,
}: {
  anchorMs?: number | null
  className?: string
}): ReactNode {
  const now = useStreamNow(anchorMs != null)
  if (anchorMs == null) return null
  return (
    <span
      className={cn('text-muted-foreground tabular-nums text-xs', className)}
      data-testid="stream-elapsed"
    >
      {formatDuration(Math.max(0, now - anchorMs))}
    </span>
  )
}

/** 状态行活性指示：统一图标 + 状态文案 + 实时总耗时 */
export function StreamActivityIndicator({
  label,
  anchorMs,
  className,
  testId,
}: {
  label: string
  anchorMs?: number | null
  className?: string
  testId?: string
}): ReactNode {
  return (
    <div className={cn('flex items-center gap-2', className)} data-testid={testId}>
      <StreamSpinner />
      <span className="text-sm">{label}</span>
      <StreamElapsedText anchorMs={anchorMs} />
    </div>
  )
}
