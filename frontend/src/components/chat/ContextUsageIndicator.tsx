/**
 * 上下文使用进度指示器（圈型进度）
 *
 * 用户决策（2026-08-14）：输入框上下文用量由横向进度条改为圈型进度
 * （AI app 标准，如 ChatGPT/Claude 的 token 圆环）——横向空间占用小，
 * 大字体主题/插件动作多时不易挤出发送按钮。
 *
 * 展示：模型名 | 圆环 | 已用 / 上限；maxTokens <= 0 时只显示模型名（不展示假进度）。
 * 语义：>=90% error 红 / >=70% warning 黄 / 其余 success 绿。
 */

import { AlertCircle, Database } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { formatNumber } from '@/utils/format'

export interface ContextUsageIndicatorProps {
  /** 模型名；空则显示「模型无效」 */
  modelName?: string | null
  /** 已使用上下文 token（通常为 promptTokens） */
  currentTokenUsage?: number
  /** 上下文窗口上限；<=0 时不显示进度 */
  maxTokens?: number
  /** 总 token（本轮输入+输出，悬停详情） */
  totalTokens?: number
  /** 本轮缓存命中 token（悬停详情） */
  cachedTokens?: number
  /** 本轮缓存命中率 0-1（悬停详情） */
  hitRatio?: number
  /** 紧凑模式（小尺寸，侧栏/旧 StatusBar 场景） */
  compact?: boolean
  className?: string
}

/** 圆环尺寸（compact 12 / 常规 16） */
const RING_SIZE = 16
const RING_SIZE_COMPACT = 12

/**
 * 圈型进度（SVG 圆环）：
 * role=progressbar 供无障碍；颜色语义随使用率（error/warning/success）。
 */
function UsageRing({ ratio, size, tone }: { ratio: number; size: number; tone: string }) {
  const stroke = 2
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const clamped = Math.max(0, Math.min(1, ratio))

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="progressbar"
      aria-valuenow={Math.round(clamped * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="上下文用量"
      className="shrink-0"
      data-testid="context-usage-ring"
    >
      {/* 轨道 */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="currentColor"
        className="text-primary/15"
        strokeWidth={stroke}
      />
      {/* 进度（从 12 点方向顺时针） */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="currentColor"
        className={cn(tone)}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={c * (1 - clamped)}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: 'stroke-dashoffset 0.3s ease' }}
      />
    </svg>
  )
}

/**
 * 模型名 + 圈型上下文进度（与原 ChatInput 输入栏一致）
 */
export function ContextUsageIndicator({
  modelName,
  currentTokenUsage = 0,
  maxTokens = 0,
  totalTokens = 0,
  cachedTokens = 0,
  hitRatio = 0,
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
        <AlertCircle className={compact ? 'h-icon-xs w-icon-xs' : 'h-icon-sm w-icon-sm'} />
        <span>模型无效</span>
      </div>
    )
  }

  const ratio = maxTokens > 0 ? currentTokenUsage / maxTokens : 0
  const tone =
    ratio >= 0.9
      ? 'text-status-error'
      : ratio >= 0.7
        ? 'text-status-warning'
        : 'text-status-success'

  // 悬停详情：上下文 + 总 token + 缓存命中（有值才显示段）
  const detailParts: string[] = []
  if (maxTokens > 0) {
    detailParts.push(`上下文 ${formatNumber(currentTokenUsage)} / ${formatNumber(maxTokens)}`)
  }
  if (totalTokens > 0) {
    detailParts.push(`总 ${formatNumber(totalTokens)} tok`)
  }
  if (cachedTokens > 0) {
    detailParts.push(`缓存命中 ${formatNumber(cachedTokens)} tok`)
  } else if (hitRatio > 0) {
    detailParts.push(`缓存命中率 ${Math.round(hitRatio * 100)}%`)
  }
  const titleText = detailParts.length > 0 ? detailParts.join(' · ') : modelName

  return (
    <div
      className={cn(
        'bg-primary/10 border-primary/20 min-w-0 items-center gap-2 rounded-lg border',
        compact
          ? 'flex h-[18px] gap-1.5 px-1.5 text-[10px]'
          : 'hidden h-8 px-3 text-xs sm:flex',
        className,
      )}
      data-testid="context-usage-indicator"
      title={titleText}
    >
      <Database
        className={cn('text-primary shrink-0', compact ? 'h-icon-xs w-icon-xs' : 'h-icon-sm w-icon-sm')}
      />
      <span className="text-primary max-w-[120px] truncate font-semibold">{modelName}</span>
      {maxTokens > 0 && <UsageRing ratio={ratio} size={compact ? RING_SIZE_COMPACT : RING_SIZE} tone={tone} />}
    </div>
  )
}
