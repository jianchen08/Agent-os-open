/**
 * 加载状态组件
 *
 * 提供多种加载状态的UI展示
 */

import { cn } from '@/lib/utils'

/**
 * 加载中组件属性
 */
export interface LoadingFallbackProps {
  /** 加载提示文字 */
  message?: string
  /** 自定义类名 */
  className?: string
  /** 加载器大小 */
  size?: 'sm' | 'md' | 'lg'
}

/**
 * 加载中组件
 *
 * 用于显示加载状态，配合 React.lazy 和 Suspense 使用
 */
export function LoadingFallback({
  message = '加载中...',
  className,
  size = 'md',
}: LoadingFallbackProps) {
  const sizeClasses = {
    sm: 'h-6 w-6',
    md: 'h-8 w-8',
    lg: 'h-12 w-12',
  }

  return (
    <div className={cn('flex items-center justify-center p-8', className)}>
      <div className="text-center">
        <div
          className={cn(
            'border-primary inline-block animate-spin rounded-full border-2 border-t-transparent',
            sizeClasses[size],
          )}
        />
        {message && <p className="text-text-secondary mt-2 text-sm">{message}</p>}
      </div>
    </div>
  )
}

/**
 * 骨架屏组件属性
 */
export interface SkeletonLoaderProps {
  /** 骨架屏数量 */
  count?: number
  /** 自定义类名 */
  className?: string
}

/**
 * 骨架屏组件
 *
 * 用于在内容加载时显示占位符
 */
export function SkeletonLoader({ count = 3, className }: SkeletonLoaderProps) {
  return (
    <div className={cn('space-y-3', className)}>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="animate-pulse">
          <div className="bg-bg-tertiary mb-2 h-4 w-3/4 rounded" />
          <div className="bg-bg-tertiary h-3 w-1/2 rounded" />
        </div>
      ))}
    </div>
  )
}

/**
 * 卡片骨架屏组件
 *
 * 用于卡片列表的骨架屏
 */
export function CardSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div className="space-y-4">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="bg-card animate-pulse rounded-lg border p-4">
          <div className="flex items-start gap-3">
            <div className="bg-bg-tertiary h-5 w-5 rounded" />
            <div className="flex-1 space-y-2">
              <div className="bg-bg-tertiary h-4 w-3/4 rounded" />
              <div className="bg-bg-tertiary h-3 w-1/2 rounded" />
            </div>
          </div>
          <div className="mt-4 space-y-2">
            <div className="bg-bg-tertiary h-2 w-full rounded" />
            <div className="bg-bg-tertiary h-2 w-2/3 rounded" />
          </div>
        </div>
      ))}
    </div>
  )
}

/**
 * 列表骨架屏组件
 *
 * 用于列表项的骨架屏
 */
export function ListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="bg-bg-secondary flex animate-pulse items-center gap-3 rounded-lg p-3"
        >
          <div className="bg-bg-tertiary h-4 w-4 rounded" />
          <div className="flex-1 space-y-1">
            <div className="bg-bg-tertiary h-3 w-1/3 rounded" />
            <div className="bg-bg-tertiary h-2 w-1/4 rounded" />
          </div>
        </div>
      ))}
    </div>
  )
}
