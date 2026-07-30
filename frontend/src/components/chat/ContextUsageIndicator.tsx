/**
 * 上下文使用进度指示器
 *
 * 与 ChatInput 原有模型旁展示一致：
 * 模型名 | 进度条 | 已用 / 上限
 *
 * maxTokens <= 0 时只显示模型名（不展示假进度）。
 */

import { AlertCircle, Database } from '@/assets/icons'
import { cn } from '@/lib/utils'

export interface ContextUsageIndicatorProps {
  /** 模型名；空则显示「模型无效」 */
  modelName?: string | null
  /** 已使用上下文 token（通常为 promptTokens） */
  currentTokenUsage?: number
  /** 上下文窗口上限；<=0 时不显示进度条 */
  maxTokens?: number
  /** 紧凑模式（StatusBar 22px 用） */
  compact?: boolean
  className?: string
}

function formatNumber(num: number): string {
  return num.toLocaleString('en-US')
}

/**
 * 模型名 + 上下文进度条（与原 ChatInput 输入栏一致）
 */
export function ContextUsageIndicator({
  modelName,
  currentTokenUsage = 0,
  maxTokens = 0,
  compact = false,
  className,
}: ContextUsageIndicatorProps) {
  if (!modelName || modelName === 'unknown') {
    return (
      <div
        className={cn(
          'items-center gap-1.5 rounded-lg border border-muted/20 text-muted-foreground',
          compact
            ? 'flex h-[18px] px-1.5 text-[10px]'
            : 'hidden h-8 px-3 text-xs sm:flex',
          className,
        )}
        data-testid="context-usage-invalid"
      >
        <AlertCircle className={compact ? 'h-3 w-3' : 'h-3.5 w-3.5'} />
        <span>模型无效</span>
      </div>
    )
  }

  const ratio = maxTokens > 0 ? currentTokenUsage / maxTokens : 0
  const barColor =
    ratio >= 0.9
      ? 'bg-status-error'
      : ratio >= 0.7
        ? 'bg-status-warning'
        : 'bg-status-success'

  return (
    <div
      className={cn(
        'bg-primary/10 border-primary/20 items-center gap-2 rounded-lg border',
        compact
          ? 'flex h-[18px] gap-1.5 px-1.5 text-[10px]'
          : 'hidden h-8 px-3 text-xs sm:flex',
        className,
      )}
      data-testid="context-usage-indicator"
      title={
        maxTokens > 0
          ? `上下文 ${formatNumber(currentTokenUsage)} / ${formatNumber(maxTokens)}`
          : modelName
      }
    >
      <Database
        className={cn('text-primary shrink-0', compact ? 'h-3 w-3' : 'h-3.5 w-3.5')}
      />
      <span className="text-primary max-w-[120px] truncate font-semibold">{modelName}</span>
      {maxTokens > 0 && (
        <>
          <span className="text-primary/40">|</span>
          <div
            className={cn(
              'bg-primary/20 overflow-hidden rounded-full',
              compact ? 'h-1 w-12' : 'h-1.5 w-20',
            )}
          >
            <div
              className={cn('h-full rounded-full transition-all duration-300', barColor)}
              style={{ width: `${Math.min(ratio * 100, 100)}%` }}
            />
          </div>
          <span className="text-primary font-medium tabular-nums">
            {formatNumber(currentTokenUsage)}
          </span>
          <span className="text-primary/50">/</span>
          <span className="text-primary/70 tabular-nums">{formatNumber(maxTokens)}</span>
        </>
      )}
    </div>
  )
}
