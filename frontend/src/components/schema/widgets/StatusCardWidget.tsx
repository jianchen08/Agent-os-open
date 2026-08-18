/**
 * 卡片 widget（统一状态/进度/任务卡三形态）
 *
 * 一个组件 × 声明参数：按 props 推断呈现形态（原 ProgressWidget/TaskCardWidget
 * 已并入，注册名 progress/task_card 作为别名仍指向本组件，声明兼容）。
 *
 * 形态推断（variant 显式指定优先）：
 * - progress：steps 多步骤 / indeterminate 不确定动画 / 单进度条（label+value）
 * - task：title + status（任务状态词表）+ progress 0-100
 * - metric（默认）：单指标/多指标 + 趋势指示（widget_event 推送可覆盖 value）
 *
 * @module StatusCardWidget
 */

import React from 'react'
import { AlertCircle, CheckCircle2, Clock, Loader2, PauseCircle, XCircle } from '@/assets/icons'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { DataWidgetStatus, useDataWidget } from '@/services/schema/dataWidget'

// ═════════════════════════════════════════════════════════════════
// metric 形态（原 StatusCardWidget）
// ═════════════════════════════════════════════════════════════════

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

// ═════════════════════════════════════════════════════════════════
// progress 形态（原 ProgressWidget）
// ═════════════════════════════════════════════════════════════════

/** 进度状态 */
type ProgressStatus = 'active' | 'success' | 'error' | 'warning'

/** 进度步骤项 */
interface ProgressStep {
  /** 步骤标签 */
  label: string
  /** 步骤值（0-100） */
  value?: number
  /** 步骤状态 */
  status?: ProgressStatus
}

/** 状态颜色映射 */
const STATUS_COLORS: Record<ProgressStatus, { bar: string; bg: string; text: string }> = {
  active: { bar: 'bg-status-info', bg: 'bg-status-info/20', text: 'text-status-info' },
  success: { bar: 'bg-status-success', bg: 'bg-status-success/20', text: 'text-status-success' },
  error: { bar: 'bg-status-error', bg: 'bg-status-error/20', text: 'text-status-error' },
  warning: { bar: 'bg-status-warning', bg: 'bg-status-warning/20', text: 'text-status-warning' },
}

/**
 * 提取步骤数组
 *
 * @param steps - 原始步骤数据
 * @returns 类型安全的 ProgressStep 数组
 */
function extractSteps(steps: unknown): ProgressStep[] {
  if (!Array.isArray(steps)) return []
  return steps.filter(
    (s): s is ProgressStep =>
      typeof s === 'object' && s !== null && typeof (s as ProgressStep).label === 'string',
  )
}

/** 进度条形态：多步骤 / 不确定 / 单条 */
function ProgressView({
  value,
  label,
  status,
  steps,
  indeterminate,
}: {
  value: number | undefined
  label: string | undefined
  status: ProgressStatus
  steps: ProgressStep[]
  indeterminate: boolean
}): React.ReactNode {
  const colors = STATUS_COLORS[status] ?? STATUS_COLORS.active

  // 多步骤模式
  if (steps.length > 0) {
    return (
      <div className="w-full space-y-3 rounded-lg border p-4">
        {label && <h3 className="text-foreground text-sm font-semibold">{label}</h3>}
        <div className="space-y-2">
          {steps.map((step, index) => {
            const stepColors = STATUS_COLORS[step.status ?? 'active'] ?? STATUS_COLORS.active
            const clampedValue = Math.max(0, Math.min(100, step.value ?? 0))
            return (
              <div key={index} className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-foreground text-sm">{step.label}</span>
                  <span className={`text-xs font-medium ${stepColors.text}`}>{clampedValue}%</span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full rounded-full transition-all duration-500 ease-out ${stepColors.bar}`}
                    style={{ width: `${clampedValue}%` }}
                  />
                </div>
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  // 不确定进度模式
  if (indeterminate) {
    return (
      <div className="w-full space-y-2 rounded-lg border p-4">
        {label && (
          <div className="flex items-center gap-2">
            <span className="text-foreground text-sm font-medium">{label}</span>
            <span className="text-muted-foreground text-xs">处理中...</span>
          </div>
        )}
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div className="bg-primary h-full w-1/3 animate-pulse rounded-full" />
        </div>
      </div>
    )
  }

  // 单进度条模式
  const clampedValue = typeof value === 'number' ? Math.max(0, Math.min(100, value)) : 0
  return (
    <div className="w-full space-y-2">
      {(label || typeof value === 'number') && (
        <div className="flex items-center justify-between">
          {label && <span className="text-foreground text-sm font-medium">{label}</span>}
          <span className={`text-xs font-medium ${colors.text}`}>{clampedValue}%</span>
        </div>
      )}
      <div className="h-2.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={`h-full rounded-full transition-all duration-500 ease-out ${colors.bar}`}
          style={{ width: `${clampedValue}%` }}
        />
      </div>
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════
// task 形态（原 TaskCardWidget）
// ═════════════════════════════════════════════════════════════════

/** 任务状态（与全局 TaskStatus 对齐的子集，宽松匹配） */
type TaskCardStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'suspended'
  | 'cancelled'
  | 'evaluating'
  | 'scheduled'
  | 'timeout'

/** 状态 → 样式/图标/文案 */
const TASK_STATUS_META: Record<TaskCardStatus, { color: string; bg: string; icon: React.ReactNode; label: string }> = {
  pending: { color: 'text-status-pending', bg: 'bg-status-pending/10', icon: <Clock className="h-4 w-4" />, label: '待执行' },
  running: { color: 'text-status-info', bg: 'bg-status-info/10', icon: <Loader2 className="h-4 w-4 animate-spin" />, label: '执行中' },
  evaluating: { color: 'text-status-info', bg: 'bg-status-info/10', icon: <Loader2 className="h-4 w-4 animate-spin" />, label: '评估中' },
  scheduled: { color: 'text-status-pending', bg: 'bg-status-pending/10', icon: <Clock className="h-4 w-4" />, label: '已调度' },
  completed: { color: 'text-status-success', bg: 'bg-status-success/10', icon: <CheckCircle2 className="h-4 w-4" />, label: '已完成' },
  failed: { color: 'text-status-error', bg: 'bg-status-error/10', icon: <AlertCircle className="h-4 w-4" />, label: '已失败' },
  blocked: { color: 'text-status-warning', bg: 'bg-status-warning/10', icon: <AlertCircle className="h-4 w-4" />, label: '已阻塞' },
  suspended: { color: 'text-status-warning', bg: 'bg-status-warning/10', icon: <PauseCircle className="h-4 w-4" />, label: '已暂停' },
  cancelled: { color: 'text-muted-foreground', bg: 'bg-muted', icon: <XCircle className="h-4 w-4" />, label: '已取消' },
  timeout: { color: 'text-status-error', bg: 'bg-status-error/10', icon: <AlertCircle className="h-4 w-4" />, label: '已超时' },
}

/**
 * 规范化任务状态值（容忍大小写/未知值，未知归入 pending）
 */
function normalizeTaskStatus(raw: unknown): TaskCardStatus {
  if (typeof raw !== 'string') return 'pending'
  return raw.toLowerCase() in TASK_STATUS_META
    ? (raw.toLowerCase() as TaskCardStatus)
    : 'pending'
}

/** 任务卡形态：标题 + 状态徽标 + 进度 */
function TaskCardView({
  title,
  status,
  progress,
  description,
  taskId,
}: {
  title: string
  status: TaskCardStatus
  progress: number | undefined
  description: string | undefined
  taskId: string | undefined
}): React.ReactNode {
  const meta = TASK_STATUS_META[status]
  return (
    <div className="rounded-lg border bg-background p-4 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <h4 className="text-foreground truncate text-sm font-semibold">{title}</h4>
          {taskId && <span className="text-muted-foreground text-xs">{taskId}</span>}
        </div>
        <span
          className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${meta.color} ${meta.bg}`}
        >
          {meta.icon}
          {meta.label}
        </span>
      </div>

      {description && <p className="text-muted-foreground mt-2 line-clamp-2 text-xs">{description}</p>}

      {progress !== undefined && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="text-muted-foreground">进度</span>
            <span className="text-foreground tabular-nums">{Math.round(progress)}%</span>
          </div>
          <div className="bg-muted h-1.5 w-full overflow-hidden rounded-full">
            <div
              className={`h-full rounded-full transition-all ${meta.color.replace('text-', 'bg-')}`}
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════
// metric 形态渲染 + 主组件（形态分发）
// ═════════════════════════════════════════════════════════════════

/**
 * 单个指标卡片
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
          {title && <h4 className="text-muted-foreground text-sm font-medium">{title}</h4>}
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
        {description && <p className="text-muted-foreground mt-1 text-xs">{description}</p>}
      </div>

      {trendStyle && !trendValue && trend && (
        <div className="mt-1">
          <span className={`text-sm font-medium ${trendStyle.color}`}>{trendStyle.arrow}</span>
        </div>
      )}
    </div>
  )
}

type CardVariant = 'metric' | 'progress' | 'task'

/**
 * 卡片 widget（status_card / progress / task_card 三注册名共用）
 *
 * @param props - 组件属性（形态推断见模块注释）
 * @returns 卡片渲染结果
 */
export function StatusCardWidget(props: Record<string, unknown>) {
  // 订阅 widget_event 推送（metric 形态：若有 widgetId，用 latest.data 的 value 覆盖 props）
  // 这是 metric_bindings 配置驱动推送的「最后一公里」：内核推 → store → 本组件渲染。
  const widgetId = props.widgetId as string | undefined
  const latest = useWidgetEventStore((s) => (widgetId ? s.latest[widgetId] : undefined))
  // A1a：datasourceUri（HTTP 拉，scalar 形状）→ 无 uri 回退静态 props
  const remote = useDataWidget(props, 'scalar' as const)
  const scalar = (props.datasourceUri ? remote.data : undefined) as
    | Record<string, unknown>
    | undefined

  const variant = props.variant as CardVariant | undefined
  const steps = extractSteps(props.steps)
  const indeterminate = (props.indeterminate as boolean) ?? false
  const progressNum =
    typeof props.progress === 'number' && !Number.isNaN(props.progress)
      ? Math.max(0, Math.min(100, props.progress))
      : undefined
  // scalar 数据源的 progress 覆盖（数字才生效）
  const progress =
    typeof scalar?.progress === 'number' && !Number.isNaN(scalar.progress)
      ? Math.max(0, Math.min(100, scalar.progress))
      : progressNum

  // 形态推断：variant 显式优先；特征 props 次之
  const resolved: CardVariant =
    variant
    ?? (steps.length > 0 || indeterminate
      ? 'progress'
      : progress !== undefined
        ? 'task'
        : typeof props.label === 'string' && typeof props.value === 'number'
          ? 'progress'
          : 'metric')

  if (resolved === 'progress') {
    return (
      <ProgressView
        value={props.value as number | undefined}
        label={props.label as string | undefined}
        status={(props.status as ProgressStatus) ?? 'active'}
        steps={steps}
        indeterminate={indeterminate}
      />
    )
  }

  if (resolved === 'task') {
    return (
      <TaskCardView
        title={(props.title as string) ?? '未命名任务'}
        status={normalizeTaskStatus(props.status)}
        progress={progress}
        description={props.description as string | undefined}
        taskId={props.task_id as string | undefined}
      />
    )
  }

  // metric 形态（默认）
  const metrics = extractMetrics(scalar?.metrics ?? props.metrics)
  const title = props.title as string | undefined
  // 优先用 widget_event 推送的 value（metric_bindings 场景），props 兜底（静态场景）
  const eventValue = latest?.data?.value as string | number | undefined
  // A1a：scalar 数据源的 value 覆盖（HTTP 拉场景），eventValue 仍然最高优先
  const scalarValue =
    scalar && 'value' in scalar ? (scalar.value as string | number | undefined) : props.value
  const value = eventValue ?? (scalarValue as string | number | undefined)
  const trend = props.trend as TrendDirection | undefined
  const trendValue = props.trendValue as string | undefined
  const icon = props.icon as string | undefined
  const description = props.description as string | undefined

  // 数据源加载/错误提示（A1a）：有 uri 且失败/加载中时前置展示
  if ((props.datasourceUri && remote.error) || (props.datasourceUri && remote.loading)) {
    return <DataWidgetStatus loading={remote.loading} error={remote.error} />
  }

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
