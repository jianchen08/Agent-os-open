/**
 * 可靠性评分徽章组件
 *
 * 显示 Agent 的可靠性评分 (0-100)
 */

import { Minus, Star, TrendingDown, TrendingUp } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface ReliabilityBadgeProps {
  /** 可靠性评分 (0-100) */
  score: number
  /** 尺寸 */
  size?: 'sm' | 'md' | 'lg'
  /** 是否显示趋势图标 */
  showTrend?: boolean
  /** 趋势方向 */
  trend?: 'up' | 'down' | 'stable'
  /** 自定义样式 */
  className?: string
}

/**
 * 可靠性评分徽章
 *
 * 根据评分显示不同颜色：
 * - 90+ : 金色（优秀）
 * - 70-89: 绿色（良好）
 * - 50-69: 黄色（一般）
 * - <50 : 红色（较差）
 */
export function ReliabilityBadge({
  score,
  size = 'md',
  showTrend = false,
  trend,
  className,
}: ReliabilityBadgeProps) {
  /** 根据评分确定颜色 */
  const getScoreColor = () => {
    if (score >= 90) return 'bg-[var(--badge-warning-bg)] text-[var(--badge-warning-text)] border-[var(--badge-warning-text)]/30'
    if (score >= 70) return 'bg-[var(--badge-success-bg)] text-[var(--badge-success-text)] border-[var(--badge-success-text)]/30'
    if (score >= 50) return 'bg-[var(--badge-warning-bg)] text-[var(--badge-warning-text)] border-[var(--badge-warning-text)]/30'
    return 'bg-[var(--badge-error-bg)] text-[var(--badge-error-text)] border-[var(--badge-error-text)]/30'
  }

  const sizeStyles = {
    sm: 'text-xs px-1.5 py-0.5 gap-1',
    md: 'text-sm px-2 py-1 gap-1.5',
    lg: 'text-base px-3 py-1.5 gap-2',
  }

  const TrendIcon = trend === 'up' ? TrendingUp : trend === 'down' ? TrendingDown : Minus

  return (
    <div
      className={cn(
        'font-code inline-flex items-center rounded-full border',
        getScoreColor(),
        sizeStyles[size],
        className,
      )}
    >
      {score >= 90 && (
        <Star
          className={cn(
            'fill-current',
            size === 'sm' && 'h-3 w-3',
            size === 'md' && 'h-3.5 w-3.5',
            size === 'lg' && 'h-4 w-4',
          )}
        />
      )}

      <span>{Math.round(score)}%</span>

      {showTrend && trend && (
        <TrendIcon
          className={cn(
            size === 'sm' && 'h-3 w-3',
            size === 'md' && 'h-3.5 w-3.5',
            size === 'lg' && 'h-4 w-4',
            trend === 'up' && 'text-status-success',
            trend === 'down' && 'text-status-error',
            trend === 'stable' && 'text-text-muted',
          )}
        />
      )}
    </div>
  )
}

/**
 * 可靠性评分详情组件
 */
export interface ReliabilityDetailProps {
  /** 可靠性评分 */
  score: number
  /** 总执行次数 */
  totalExecutions: number
  /** 成功率 */
  successRate: number
  /** 平均重试次数 */
  avgRetries: number
  /** 最后更新时间 */
  lastUpdated?: string
}

/**
 * 可靠性评分详情
 */
export function ReliabilityDetail({
  score,
  totalExecutions,
  successRate,
  avgRetries,
  lastUpdated,
}: ReliabilityDetailProps) {
  return (
    <div className="glass-panel space-y-3 rounded-lg p-4">
      <div className="flex items-center justify-between">
        <span className="text-text-secondary">可靠性评分</span>
        <ReliabilityBadge score={score} size="lg" />
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="flex justify-between">
          <span className="text-text-muted">执行次数</span>
          <span className="text-text-primary font-code">{totalExecutions}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-text-muted">成功率</span>
          <span className="text-text-primary font-code">{(successRate * 100).toFixed(1)}%</span>
        </div>
        <div className="flex justify-between">
          <span className="text-text-muted">平均重试</span>
          <span className="text-text-primary font-code">{avgRetries.toFixed(1)}</span>
        </div>
        {lastUpdated && (
          <div className="flex justify-between">
            <span className="text-text-muted">更新时间</span>
            <span className="text-text-primary text-xs">{lastUpdated}</span>
          </div>
        )}
      </div>
    </div>
  )
}
