/**
 * 状态卡片组件
 *
 * 根据 Schema 渲染状态信息卡片，支持单指标和多指标模式，
 * 包含图标、标题、数值和趋势指示。
 *
 * @module StatusCardWidget
 */

import React from 'react'

/** 趋势方向 */
type TrendDirection = 'up' | 'down' | 'flat'

/** 指标项定义 */
interface MetricItem {
  /** 指标标题 */
  title: string
  /** 指标值 */
  value: string | number
  /** 趋势方向 */
  trend?: TrendDirection
  /** 趋势值（如 +12.5%） */
  trendValue?: string
  /** 图标 */
  icon?: string
  /** 描述 */
  description?: string
}

/** 趋势颜色映射 */
const TREND_STYLES: Record<TrendDirection, { color: string; arrow: string; bg: string }> = {
  up: { color: 'text-status-success', arrow: '↑', bg: 'bg-status-success/10' },
  down: { color: 'text-status-error', arrow: '↓', bg: 'bg-status-error/10' },
  flat: { color: 'text-status-pending', arrow: '→', bg: 'bg-status-pending/10' },
}

/**
 * 提取指标数组
 *
 * @param metrics - 原始指标数据
 * @returns 类型安全的 MetricItem 数组
 */
function extractMetrics(metrics: unknown): MetricItem[] {
  if (!Array.isArray(metrics)) return []
  return metrics.filter(
    (m): m is MetricItem =>
      typeof m === 'object' && m !== null && typeof (m as MetricItem).title === 'string',
  )
}

/**
 * 状态卡片组件
 *
 * 支持单指标卡片和多指标卡片组，包含趋势指示和图标。
 *
 * @param props - 组件属性，包含 title、value、trend、metrics 等
 * @returns 状态卡片渲染结果
 */
export function StatusCardWidget(props: Record<string, unknown>) {
  const metrics = extractMetrics(props.metrics)
  const title = props.title as string | undefined
  const value = props.value as string | number | undefined
  const trend = props.trend as TrendDirection | undefined
  const trendValue = props.trendValue as string | undefined
  const icon = props.icon as string | undefined
  const description = props.description as string | undefined

  // 多指标模式
  if (metrics.length > 0) {
    return (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {metrics.map((metric, index) => (
          <StatusCard
            key={index}
            title={metric.title}
            value={metric.value}
            trend={metric.trend}
            trendValue={metric.trendValue}
            icon={metric.icon}
            description={metric.description}
          />
        ))}
      </div>
    )
  }

  // 单指标模式
  return (
    <StatusCard
      title={title}
      value={value ?? '—'}
      trend={trend}
      trendValue={trendValue}
      icon={icon}
      description={description}
    />
  )
}

/**
 * 单个状态卡片
 *
 * @param params - 卡片属性
 * @returns 卡片 JSX
 */
function StatusCard({
  title,
  value,
  trend,
  trendValue,
  icon,
  description,
}: {
  title?: string
  value: string | number
  trend?: TrendDirection
  trendValue?: string
  icon?: string
  description?: string
}): React.ReactNode {
  const trendStyle = trend ? (TREND_STYLES[trend] ?? TREND_STYLES.flat) : null

  return (
    <div className="rounded-lg border bg-background p-4 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          {icon && (
            <div className="bg-primary/10 text-primary flex h-8 w-8 items-center justify-center rounded-lg text-lg">
              {icon}
            </div>
          )}
          {title && (
            <h4 className="text-muted-foreground text-sm font-medium">{title}</h4>
          )}
        </div>
        {trendStyle && trendValue && (
          <span
            className={`inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 text-xs font-medium ${trendStyle.color} ${trendStyle.bg}`}
          >
            {trendStyle.arrow} {trendValue}
          </span>
        )}
      </div>

      <div className="mt-2">
        <p className="text-foreground text-2xl font-bold tabular-nums">{value}</p>
        {description && (
          <p className="text-muted-foreground mt-1 text-xs">{description}</p>
        )}
      </div>

      {trendStyle && !trendValue && trend && (
        <div className="mt-1">
          <span className={`text-sm font-medium ${trendStyle.color}`}>
            {trendStyle.arrow}
          </span>
        </div>
      )}
    </div>
  )
}
