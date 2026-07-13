/**
 * 进度展示组件
 *
 * 根据 Schema 渲染进度条，支持多种状态、多步骤进度和不确定进度动画。
 *
 * @module ProgressWidget
 */

import React from 'react'

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

/**
 * 进度展示组件
 *
 * 支持单个进度条、多步骤进度和不确定进度（loading 动画）。
 *
 * @param props - 组件属性，包含 value、label、status、steps 等
 * @returns 进度条渲染结果
 */
export function ProgressWidget(props: Record<string, unknown>) {
  const value = props.value as number | undefined
  const label = props.label as string | undefined
  const status = (props.status as ProgressStatus) ?? 'active'
  const steps = extractSteps(props.steps)
  const indeterminate = (props.indeterminate as boolean) ?? false

  const colors = STATUS_COLORS[status] ?? STATUS_COLORS.active

  // 多步骤模式
  if (steps.length > 0) {
    return (
      <div className="w-full space-y-3 rounded-lg border p-4">
        {label && (
          <h3 className="text-foreground text-sm font-semibold">{label}</h3>
        )}
        <div className="space-y-2">
          {steps.map((step, index) => {
            const stepColors = STATUS_COLORS[step.status ?? 'active'] ?? STATUS_COLORS.active
            const stepValue = step.value ?? 0
            const clampedValue = Math.max(0, Math.min(100, stepValue))

            return (
              <div key={index} className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-foreground text-sm">{step.label}</span>
                  <span className={`text-xs font-medium ${stepColors.text}`}>
                    {clampedValue}%
                  </span>
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
  const clampedValue =
    typeof value === 'number' ? Math.max(0, Math.min(100, value)) : 0

  return (
    <div className="w-full space-y-2">
      {(label || typeof value === 'number') && (
        <div className="flex items-center justify-between">
          {label && (
            <span className="text-foreground text-sm font-medium">{label}</span>
          )}
          <span className={`text-xs font-medium ${colors.text}`}>
            {clampedValue}%
          </span>
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
