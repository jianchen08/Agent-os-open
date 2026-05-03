/**
 * 图表展示组件
 *
 * 根据 Schema 渲染各类图表，使用纯 SVG/CSS 实现，不依赖重型图表库。
 * 支持类型：line、bar、pie、doughnut、area、radar、scatter
 *
 * @module ChartWidget
 */

import React, { useMemo } from 'react'

/** 图表类型 */
type ChartType =
  | 'line'
  | 'bar'
  | 'pie'
  | 'doughnut'
  | 'area'
  | 'radar'
  | 'scatter'

/** 数据集定义 */
interface ChartDataset {
  /** 数据值数组 */
  data: number[]
  /** 数据集标签 */
  label?: string
  /** 数据集颜色 */
  color?: string
  /** 背景色 */
  backgroundColor?: string
}

/** 图表数据格式 */
interface ChartData {
  /** 标签数组 */
  labels: string[]
  /** 数据集数组 */
  datasets: ChartDataset[]
}

/** 图表配置 */
interface ChartConfig {
  /** 图表标题 */
  title?: string
  /** 是否显示图例 */
  showLegend?: boolean
  /** 是否显示 tooltip */
  showTooltip?: boolean
  /** 宽度 */
  width?: number | string
  /** 高度 */
  height?: number | string
}

/** 预置颜色方案 */
const PALETTE = [
  '#3b82f6', // blue-500
  '#ef4444', // red-500
  '#22c55e', // green-500
  '#f59e0b', // amber-500
  '#8b5cf6', // violet-500
  '#06b6d4', // cyan-500
  '#ec4899', // pink-500
  '#14b8a6', // teal-500
]

/**
 * 提取图表数据
 *
 * @param data - 原始数据
 * @returns 标准化 ChartData
 */
function extractData(data: unknown): ChartData {
  if (!data || typeof data !== 'object') return { labels: [], datasets: [] }
  const d = data as Record<string, unknown>
  const labels = Array.isArray(d.labels)
    ? d.labels.map(String)
    : []
  const datasets = Array.isArray(d.datasets)
    ? d.datasets.map((ds: unknown, i: number) => {
        const dsObj = ds as Record<string, unknown>
        return {
          data: Array.isArray(dsObj.data)
            ? dsObj.data.map(Number)
            : [],
          label: String(dsObj.label ?? `Dataset ${i + 1}`),
          color: (dsObj.color as string) ?? PALETTE[i % PALETTE.length],
          backgroundColor:
            (dsObj.backgroundColor as string) ?? PALETTE[i % PALETTE.length],
        }
      })
    : []
  return { labels, datasets }
}

/**
 * 提取图表配置
 *
 * @param props - 组件属性
 * @returns 标准化 ChartConfig
 */
function extractConfig(props: Record<string, unknown>): ChartConfig {
  return {
    title: props.title as string | undefined,
    showLegend: (props.showLegend as boolean) ?? true,
    showTooltip: (props.showTooltip as boolean) ?? true,
    width: (props.width as number | string) ?? '100%',
    height: (props.height as number | string) ?? 280,
  }
}

/**
 * 图表展示组件
 *
 * 纯 SVG/CSS 实现的基础图表组件，支持多种图表类型。
 *
 * @param props - 组件属性，包含 chartType、data、title 等
 * @returns 图表渲染结果
 */
export function ChartWidget(props: Record<string, unknown>) {
  const chartType = (props.chartType as ChartType) ?? 'bar'
  const data = extractData(props.data)
  const config = extractConfig(props)

  const hasData =
    data.labels.length > 0 && data.datasets.some((ds) => ds.data.length > 0)

  if (!hasData) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border p-8">
        <svg
          className="text-muted-foreground mb-2 h-12 w-12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <path d="M3 3v18h18" />
          <path d="M7 16l4-4 4 4 4-8" />
        </svg>
        <p className="text-muted-foreground text-sm">暂无图表数据</p>
      </div>
    )
  }

  return (
    <div className="w-full rounded-lg border p-4">
      {config.title && (
        <h3 className="text-foreground mb-3 text-base font-semibold">
          {config.title}
        </h3>
      )}
      <div style={{ width: config.width, height: config.height }}>
        {renderChart(chartType, data, config)}
      </div>
      {config.showLegend && data.datasets.length > 1 && (
        <div className="mt-3 flex flex-wrap gap-3">
          {data.datasets.map((ds, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <span
                className="inline-block h-3 w-3 rounded-sm"
                style={{ backgroundColor: ds.color ?? PALETTE[i] }}
              />
              <span className="text-muted-foreground text-xs">{ds.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 根据图表类型分发渲染
 *
 * @param type - 图表类型
 * @param data - 图表数据
 * @param config - 图表配置
 * @returns 图表 SVG JSX
 */
function renderChart(
  type: ChartType,
  data: ChartData,
  config: ChartConfig,
): React.ReactNode {
  switch (type) {
    case 'line':
    case 'area':
      return <LineAreaChart data={data} config={config} filled={type === 'area'} />
    case 'bar':
      return <BarChart data={data} config={config} />
    case 'pie':
      return <PieChart data={data} config={config} />
    case 'doughnut':
      return <PieChart data={data} config={config} doughnut />
    case 'radar':
      return <RadarChart data={data} config={config} />
    case 'scatter':
      return <ScatterChart data={data} config={config} />
    default:
      return <BarChart data={data} config={config} />
  }
}

/** 折线/面积图 */
function LineAreaChart({
  data,
  config,
  filled,
}: {
  data: ChartData
  config: ChartConfig
  filled?: boolean
}): React.ReactNode {
  const height = typeof config.height === 'number' ? config.height : 280
  const padding = { top: 20, right: 20, bottom: 40, left: 40 }
  const chartW = 400
  const chartH = height - padding.top - padding.bottom

  const allValues = data.datasets.flatMap((ds) => ds.data)
  const maxVal = Math.max(...allValues, 1)
  const minVal = Math.min(...allValues, 0)
  const range = maxVal - minVal || 1

  const points = useMemo(() => {
    return data.datasets.map((ds) => {
      const pts = ds.data.map((v, i) => ({
        x:
          padding.left +
          (data.labels.length > 1
            ? (i / (data.labels.length - 1)) *
              (chartW - padding.left - padding.right)
            : 0),
        y: padding.top + chartH - ((v - minVal) / range) * chartH,
      }))
      return { dataset: ds, points: pts }
    })
  }, [data, chartH, minVal, range])

  return (
    <svg viewBox={`0 0 ${chartW} ${height}`} className="h-full w-full">
      {/* 网格线 */}
      {Array.from({ length: 5 }, (_, i) => {
        const y = padding.top + (chartH / 4) * i
        const val = maxVal - ((maxVal - minVal) / 4) * i
        return (
          <g key={i}>
            <line
              x1={padding.left}
              y1={y}
              x2={chartW - padding.right}
              y2={y}
              className="stroke-border"
              strokeWidth={0.5}
            />
            <text
              x={padding.left - 6}
              y={y + 4}
              className="fill-muted-foreground"
              fontSize={10}
              textAnchor="end"
            >
              {val.toFixed(0)}
            </text>
          </g>
        )
      })}

      {/* X轴标签 */}
      {data.labels.map((label, i) => {
        const x =
          padding.left +
          (data.labels.length > 1
            ? (i / (data.labels.length - 1)) *
              (chartW - padding.left - padding.right)
            : 0)
        return (
          <text
            key={i}
            x={x}
            y={height - 8}
            className="fill-muted-foreground"
            fontSize={10}
            textAnchor="middle"
          >
            {label.length > 6 ? label.slice(0, 6) + '…' : label}
          </text>
        )
      })}

      {/* 数据线/面积 */}
      {points.map(({ dataset, points: pts }, di) => {
        if (pts.length === 0) return null
        const linePath = pts
          .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`)
          .join(' ')

        return (
          <g key={di}>
            {filled && pts.length > 1 && (
              <path
                d={`${linePath} L${pts[pts.length - 1].x},${padding.top + chartH} L${pts[0].x},${padding.top + chartH} Z`}
                fill={dataset.color ?? PALETTE[di]}
                fillOpacity={0.15}
              />
            )}
            {pts.length > 1 && (
              <path
                d={linePath}
                fill="none"
                stroke={dataset.color ?? PALETTE[di]}
                strokeWidth={2}
              />
            )}
            {pts.map((p, pi) => (
              <circle
                key={pi}
                cx={p.x}
                cy={p.y}
                r={3}
                fill={dataset.color ?? PALETTE[di]}
              />
            ))}
          </g>
        )
      })}
    </svg>
  )
}

/** 柱状图 */
function BarChart({
  data,
  config,
}: {
  data: ChartData
  config: ChartConfig
}): React.ReactNode {
  const height = typeof config.height === 'number' ? config.height : 280
  const padding = { top: 20, right: 20, bottom: 40, left: 40 }
  const chartW = 400
  const chartH = height - padding.top - padding.bottom
  const drawableW = chartW - padding.left - padding.right

  const allValues = data.datasets.flatMap((ds) => ds.data)
  const maxVal = Math.max(...allValues, 1)

  const barGroupWidth = drawableW / data.labels.length
  const barWidth =
    data.datasets.length > 1
      ? barGroupWidth / data.datasets.length - 2
      : barGroupWidth * 0.6

  return (
    <svg viewBox={`0 0 ${chartW} ${height}`} className="h-full w-full">
      {/* 网格线 */}
      {Array.from({ length: 5 }, (_, i) => {
        const y = padding.top + (chartH / 4) * i
        const val = maxVal - (maxVal / 4) * i
        return (
          <g key={i}>
            <line
              x1={padding.left}
              y1={y}
              x2={chartW - padding.right}
              y2={y}
              className="stroke-border"
              strokeWidth={0.5}
            />
            <text
              x={padding.left - 6}
              y={y + 4}
              className="fill-muted-foreground"
              fontSize={10}
              textAnchor="end"
            >
              {val.toFixed(0)}
            </text>
          </g>
        )
      })}

      {/* 柱子 */}
      {data.labels.map((label, li) => {
        const groupX = padding.left + barGroupWidth * li
        return (
          <g key={li}>
            {data.datasets.map((ds, di) => {
              const value = ds.data[li] ?? 0
              const barH = (value / maxVal) * chartH
              const x =
                groupX +
                (data.datasets.length > 1
                  ? (barGroupWidth / data.datasets.length) * di + 1
                  : barGroupWidth * 0.2)
              const y = padding.top + chartH - barH
              return (
                <rect
                  key={di}
                  x={x}
                  y={y}
                  width={Math.max(barWidth, 4)}
                  height={Math.max(barH, 0)}
                  fill={ds.color ?? PALETTE[di]}
                  rx={2}
                />
              )
            })}
            <text
              x={groupX + barGroupWidth / 2}
              y={height - 8}
              className="fill-muted-foreground"
              fontSize={10}
              textAnchor="middle"
            >
              {label.length > 6 ? label.slice(0, 6) + '…' : label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

/** 饼图/环形图 */
function PieChart({
  data,
  config,
  doughnut,
}: {
  data: ChartData
  config: ChartConfig
  doughnut?: boolean
}): React.ReactNode {
  const size = typeof config.height === 'number' ? config.height : 280
  const cx = size / 2
  const cy = size / 2
  const radius = size / 2 - 20
  const innerRadius = doughnut ? radius * 0.55 : 0

  // 合并所有数据集的值用于饼图
  const values = data.labels.map((_, i) =>
    data.datasets.reduce((sum, ds) => sum + (ds.data[i] ?? 0), 0),
  )
  const total = values.reduce((a, b) => a + b, 0) || 1

  const slices = useMemo(() => {
    let angle = -Math.PI / 2
    return values.map((value, i) => {
      const sliceAngle = (value / total) * Math.PI * 2
      const startAngle = angle
      angle += sliceAngle

      const x1 = cx + radius * Math.cos(startAngle)
      const y1 = cy + radius * Math.sin(startAngle)
      const x2 = cx + radius * Math.cos(angle)
      const y2 = cy + radius * Math.sin(angle)

      const ix1 = cx + innerRadius * Math.cos(startAngle)
      const iy1 = cy + innerRadius * Math.sin(startAngle)
      const ix2 = cx + innerRadius * Math.cos(angle)
      const iy2 = cy + innerRadius * Math.sin(angle)

      const largeArc = sliceAngle > Math.PI ? 1 : 0

      const path =
        innerRadius > 0
          ? `M${ix1},${iy1} L${x1},${y1} A${radius},${radius} 0 ${largeArc},1 ${x2},${y2} L${ix2},${iy2} A${innerRadius},${innerRadius} 0 ${largeArc},0 ${ix1},${iy1} Z`
          : `M${cx},${cy} L${x1},${y1} A${radius},${radius} 0 ${largeArc},1 ${x2},${y2} Z`

      return { path, color: PALETTE[i % PALETTE.length], label: data.labels[i], value }
    })
  }, [values, total, cx, cy, radius, innerRadius, data.labels])

  return (
    <div className="flex items-center justify-center">
      <svg viewBox={`0 0 ${size} ${size}`} className="h-full w-full" style={{ maxHeight: size }}>
        {slices.map((slice, i) => (
          <path
            key={i}
            d={slice.path}
            fill={slice.color}
            stroke="var(--background, #fff)"
            strokeWidth={1}
          />
        ))}
        {/* 中心标签（环形图） */}
        {doughnut && (
          <text
            x={cx}
            y={cy}
            textAnchor="middle"
            dominantBaseline="central"
            className="fill-foreground"
            fontSize={14}
            fontWeight={600}
          >
            {total.toFixed(0)}
          </text>
        )}
      </svg>
      {/* 图例 */}
      <div className="ml-3 flex flex-col gap-1">
        {slices.map((s, i) => (
          <div key={i} className="flex items-center gap-1.5 text-xs">
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ backgroundColor: s.color }}
            />
            <span className="text-muted-foreground">
              {s.label} ({s.value})
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** 雷达图 */
function RadarChart({
  data,
  config,
}: {
  data: ChartData
  config: ChartConfig
}): React.ReactNode {
  const size = typeof config.height === 'number' ? config.height : 280
  const cx = size / 2
  const cy = size / 2
  const radius = size / 2 - 30
  const axes = data.labels.length
  if (axes < 3) {
    return (
      <div className="text-muted-foreground flex items-center justify-center p-4 text-sm">
        雷达图至少需要3个维度
      </div>
    )
  }

  const angleStep = (Math.PI * 2) / axes
  const allValues = data.datasets.flatMap((ds) => ds.data)
  const maxVal = Math.max(...allValues, 1)

  const getPoint = (index: number, value: number) => {
    const angle = angleStep * index - Math.PI / 2
    const r = (value / maxVal) * radius
    return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) }
  }

  return (
    <svg viewBox={`0 0 ${size} ${size}`} className="h-full w-full">
      {/* 网格环 */}
      {[0.2, 0.4, 0.6, 0.8, 1].map((scale, si) => (
        <polygon
          key={si}
          points={Array.from({ length: axes }, (_, i) => {
            const angle = angleStep * i - Math.PI / 2
            const r = radius * scale
            return `${cx + r * Math.cos(angle)},${cy + r * Math.sin(angle)}`
          }).join(' ')}
          fill="none"
          className="stroke-border"
          strokeWidth={0.5}
        />
      ))}

      {/* 轴线 */}
      {data.labels.map((_, i) => {
        const angle = angleStep * i - Math.PI / 2
        return (
          <line
            key={i}
            x1={cx}
            y1={cy}
            x2={cx + radius * Math.cos(angle)}
            y2={cy + radius * Math.sin(angle)}
            className="stroke-border"
            strokeWidth={0.5}
          />
        )
      })}

      {/* 数据多边形 */}
      {data.datasets.map((ds, di) => {
        const pts = ds.data
          .map((v, i) => getPoint(i, v))
          .map((p) => `${p.x},${p.y}`)
          .join(' ')
        return (
          <polygon
            key={di}
            points={pts}
            fill={ds.color ?? PALETTE[di]}
            fillOpacity={0.2}
            stroke={ds.color ?? PALETTE[di]}
            strokeWidth={2}
          />
        )
      })}

      {/* 轴标签 */}
      {data.labels.map((label, i) => {
        const angle = angleStep * i - Math.PI / 2
        const lx = cx + (radius + 16) * Math.cos(angle)
        const ly = cy + (radius + 16) * Math.sin(angle)
        return (
          <text
            key={i}
            x={lx}
            y={ly}
            className="fill-muted-foreground"
            fontSize={10}
            textAnchor="middle"
            dominantBaseline="central"
          >
            {label}
          </text>
        )
      })}
    </svg>
  )
}

/** 散点图 */
function ScatterChart({
  data,
  config,
}: {
  data: ChartData
  config: ChartConfig
}): React.ReactNode {
  const height = typeof config.height === 'number' ? config.height : 280
  const padding = { top: 20, right: 20, bottom: 40, left: 40 }
  const chartW = 400
  const chartH = height - padding.top - padding.bottom
  const drawableW = chartW - padding.left - padding.right

  const allValues = data.datasets.flatMap((ds) => ds.data)
  const maxVal = Math.max(...allValues, 1)

  return (
    <svg viewBox={`0 0 ${chartW} ${height}`} className="h-full w-full">
      {/* 网格线 */}
      {Array.from({ length: 5 }, (_, i) => {
        const y = padding.top + (chartH / 4) * i
        const val = maxVal - (maxVal / 4) * i
        return (
          <g key={i}>
            <line
              x1={padding.left}
              y1={y}
              x2={chartW - padding.right}
              y2={y}
              className="stroke-border"
              strokeWidth={0.5}
            />
            <text
              x={padding.left - 6}
              y={y + 4}
              className="fill-muted-foreground"
              fontSize={10}
              textAnchor="end"
            >
              {val.toFixed(0)}
            </text>
          </g>
        )
      })}

      {/* 散点 */}
      {data.datasets.map((ds, di) =>
        ds.data.map((value, vi) => {
          const x = padding.left + (vi / Math.max(ds.data.length - 1, 1)) * drawableW
          const y = padding.top + chartH - (value / maxVal) * chartH
          return (
            <circle
              key={`${di}-${vi}`}
              cx={x}
              cy={y}
              r={4}
              fill={ds.color ?? PALETTE[di]}
              fillOpacity={0.7}
            />
          )
        }),
      )}

      {/* X轴标签 */}
      {data.labels.map((label, i) => (
        <text
          key={i}
          x={
            padding.left +
            (data.labels.length > 1
              ? (i / (data.labels.length - 1)) * drawableW
              : drawableW / 2)
          }
          y={height - 8}
          className="fill-muted-foreground"
          fontSize={10}
          textAnchor="middle"
        >
          {label.length > 6 ? label.slice(0, 6) + '…' : label}
        </text>
      ))}
    </svg>
  )
}
