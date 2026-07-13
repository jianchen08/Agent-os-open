import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * 进度条组件
 *
 * 样式由主题配置控制：
 * - 圆角：由 --progress-radius 控制
 * - 轨道背景：由 --progress-track-bg 控制
 * - 颜色变体：由 --progress-default/success/warning/error 控制
 */

export interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  value?: number
  max?: number
  variant?: 'default' | 'success' | 'warning' | 'error'
}

const Progress = React.forwardRef<HTMLDivElement, ProgressProps>(
  ({ className, value, max = 100, variant = 'default', ...props }, ref) => {
    const percentage =
      value !== undefined ? Math.min(100, Math.max(0, (value / max) * 100)) : undefined
    const isIndeterminate = percentage === undefined

    const variantStyles: Record<string, string> = {
      default: 'var(--progress-default, #3b82f6)',
      success: 'var(--progress-success, #10b981)',
      warning: 'var(--progress-warning, #f59e0b)',
      error: 'var(--progress-error, #ef4444)',
    }

    return (
      <div
        ref={ref}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-valuetext={percentage !== undefined ? `${Math.round(percentage)}%` : '加载中'}
        className={cn('relative h-2 w-full overflow-hidden', className)}
        style={{
          borderRadius: 'var(--progress-radius, 9999px)',
          backgroundColor: 'var(--progress-track-bg, rgba(59, 130, 246, 0.2))',
        }}
        {...props}
      >
        <div
          className={cn(
            'h-full transition-all duration-300 ease-in-out',
            isIndeterminate && 'animate-progress-indeterminate',
          )}
          style={{
            width: isIndeterminate ? '40%' : `${percentage}%`,
            backgroundColor: variantStyles[variant],
            borderRadius: 'var(--progress-radius, 9999px)',
          }}
        />
      </div>
    )
  },
)
Progress.displayName = 'Progress'

export { Progress }
