/**
 * 上下文使用进度指示器（圈型进度 + 用量明细浮窗）
 *
 * 主条：模型名 | 圆环 | 已用/上限（紧凑数字）；maxTokens <= 0 时只显示模型名
 * （不展示假进度）。语义：>=90% error 红 / >=70% warning 黄 / 其余 success 绿。
 *
 * 浮窗：鼠标悬停或点击弹出（输入框在页面底部，向上弹），展示上下文使用量、
 * 本轮 token 明细（输出/总计/缓存命中）与该管道的累计 token 明细；
 * 悬停移入浮窗不关闭，点击浮窗外/再次点击关闭。
 */

import { useState } from 'react'
import { AlertCircle, Database } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { formatNumber } from '@/utils/format'
import type { CumulativeUsage } from '@/stores/contextUsageStore'

export interface ContextUsageIndicatorProps {
  /** 模型名；空则显示「模型无效」 */
  modelName?: string | null
  /** 已使用上下文 token（通常为 promptTokens） */
  currentTokenUsage?: number
  /** 上下文窗口上限；<=0 时不显示进度 */
  maxTokens?: number
  /** 本轮总 token（输入+输出，浮窗明细） */
  totalTokens?: number
  /** 本轮输出 token（浮窗明细） */
  completionTokens?: number
  /** 本轮缓存命中 token（浮窗明细） */
  cachedTokens?: number
  /** 本轮缓存命中率 0-1（浮窗明细） */
  hitRatio?: number
  /** 该管道累计消耗（浮窗明细；无累计数据不显示累计段） */
  cumulative?: CumulativeUsage
  /** 紧凑模式（小尺寸，侧栏/旧 StatusBar 场景） */
  compact?: boolean
  className?: string
}

/** 圆环尺寸（compact 12 / 常规 16） */
const RING_SIZE = 16
const RING_SIZE_COMPACT = 12

/** token 紧凑格式（主条用）：1000→1.0k，128000→128k，2500000→2.5M */
function formatCompactTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 100_000) return `${Math.round(n / 1_000)}k`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

/** 浮窗明细行（有值才渲染） */
function DetailRow({ label, value, ratio }: { label: string; value?: number; ratio?: number }) {
  if (!value || value <= 0) return null
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">
        {formatNumber(value)}
        {typeof ratio === 'number' && ratio > 0 ? ` (${Math.round(ratio * 100)}%)` : ''}
      </span>
    </div>
  )
}

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
 * 用量明细浮窗：上下文使用量 + 本轮明细 + 管道累计明细（段内无数据的行不渲染）。
 * 定位：锚点上方左对齐（输入框位于页面底部，向上弹出）。
 */
function UsagePopover({
  modelName,
  currentTokenUsage,
  maxTokens,
  totalTokens,
  completionTokens,
  cachedTokens,
  hitRatio,
  cumulative,
  onMouseEnter,
  onMouseLeave,
}: {
  modelName: string
  currentTokenUsage: number
  maxTokens: number
  totalTokens: number
  completionTokens: number
  cachedTokens: number
  hitRatio: number
  cumulative?: CumulativeUsage
  onMouseEnter: () => void
  onMouseLeave: () => void
}) {
  const ratio = maxTokens > 0 ? Math.min(1, currentTokenUsage / maxTokens) : 0
  return (
    // 外层承载定位与 hover 桥（pb-2 把视觉间隙纳入浮窗 hover 区，鼠标从主条
    // 移入浮窗不穿缝隙、不闪关）；内层是面板本体
    <div
      className="absolute bottom-full left-0 z-[100] pb-2"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      data-testid="context-usage-popover"
    >
      <div
        className="border-border bg-[var(--ds-bg-panel,hsl(var(--card)))] w-64 rounded-lg border p-3 shadow-xl"
      >
      {/* 上下文使用量 */}
      <div className="text-muted-foreground mb-1.5 text-[10px] font-medium tracking-wide">
        上下文 · {modelName}
      </div>
      {maxTokens > 0 && (
        <div className="bg-primary/15 mb-1 h-1.5 overflow-hidden rounded-full">
          <div
            className="bg-primary h-full rounded-full"
            style={{ width: `${ratio * 100}%` }}
            data-testid="context-usage-popover-bar"
          />
        </div>
      )}
      <div className="flex items-center justify-between gap-4 text-xs">
        <span className="text-muted-foreground">{maxTokens > 0 ? '已用 / 上限' : '已用'}</span>
        <span className="font-medium tabular-nums">
          {formatNumber(currentTokenUsage)}
          {maxTokens > 0 ? ` / ${formatNumber(maxTokens)}（${Math.round(ratio * 100)}%）` : ''}
        </span>
      </div>

      {/* 本轮 token 明细 */}
      {(totalTokens > 0 || completionTokens > 0 || cachedTokens > 0) && (
        <div className="border-border mt-2 border-t pt-2 text-xs">
          <div className="text-muted-foreground mb-1 text-[10px] font-medium tracking-wide">
            本轮
          </div>
          <div className="space-y-0.5">
            <DetailRow label="输入" value={currentTokenUsage} />
            <DetailRow label="输出" value={completionTokens} />
            <DetailRow label="总计" value={totalTokens} />
            <DetailRow label="缓存命中" value={cachedTokens} ratio={hitRatio} />
          </div>
        </div>
      )}

      {/* 该管道累计 token 明细 */}
      {cumulative && cumulative.total_tokens > 0 && (
        <div className="border-border mt-2 border-t pt-2 text-xs">
          <div className="text-muted-foreground mb-1 text-[10px] font-medium tracking-wide">
            本管道累计
          </div>
          <div className="space-y-0.5">
            <DetailRow label="输入" value={cumulative.total_input} />
            <DetailRow label="输出" value={cumulative.total_output} />
            <DetailRow label="总计" value={cumulative.total_tokens} />
            <DetailRow
              label="缓存命中"
              value={cumulative.total_cached}
              ratio={cumulative.cache_hit_ratio}
            />
          </div>
        </div>
      )}
      </div>
    </div>
  )
}

/**
 * 模型名 + 圈型上下文进度 + 用量明细浮窗（输入框工具栏槽位默认件）
 */
export function ContextUsageIndicator({
  modelName,
  currentTokenUsage = 0,
  maxTokens = 0,
  totalTokens = 0,
  completionTokens = 0,
  cachedTokens = 0,
  hitRatio = 0,
  cumulative,
  compact = false,
  className,
}: ContextUsageIndicatorProps) {
  const [popoverOpen, setPopoverOpen] = useState(false)

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

  return (
    <div className={cn('relative', className)}>
      <div
        className={cn(
          'bg-primary/10 border-primary/20 min-w-0 cursor-pointer items-center gap-2 rounded-lg border',
          compact
            ? 'flex h-[18px] gap-1.5 px-1.5 text-[10px]'
            : 'hidden h-8 px-3 text-xs sm:flex',
        )}
        data-testid="context-usage-indicator"
        aria-expanded={popoverOpen}
        onClick={() => setPopoverOpen((v) => !v)}
        onMouseEnter={() => setPopoverOpen(true)}
        onMouseLeave={() => setPopoverOpen(false)}
      >
        <Database
          className={cn('text-primary shrink-0', compact ? 'h-icon-xs w-icon-xs' : 'h-icon-sm w-icon-sm')}
        />
        <span className="text-primary max-w-[120px] truncate font-semibold">{modelName}</span>
        {maxTokens > 0 && <UsageRing ratio={ratio} size={compact ? RING_SIZE_COMPACT : RING_SIZE} tone={tone} />}
        {maxTokens > 0 && (
          <span className="text-primary/80 tabular-nums whitespace-nowrap">
            {formatCompactTokens(currentTokenUsage)} / {formatCompactTokens(maxTokens)}
          </span>
        )}
      </div>
      {popoverOpen && (
        <UsagePopover
          modelName={modelName}
          currentTokenUsage={currentTokenUsage}
          maxTokens={maxTokens}
          totalTokens={totalTokens}
          completionTokens={completionTokens}
          cachedTokens={cachedTokens}
          hitRatio={hitRatio}
          cumulative={cumulative}
          onMouseEnter={() => setPopoverOpen(true)}
          onMouseLeave={() => setPopoverOpen(false)}
        />
      )}
    </div>
  )
}
