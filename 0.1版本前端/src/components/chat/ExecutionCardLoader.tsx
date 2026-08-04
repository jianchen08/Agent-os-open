/**
 * 执行卡片加载器组件
 *
 * 根据执行记录 ID 查询数据并渲染活动卡片
 */

import { AlertCircle } from 'lucide-react'
import { useExecutionRecord } from '@/hooks/useExecutionRecord'
import { cn } from '@/lib/utils'
import ActivityCard from './ActivityCard'
import type { FC } from 'react'

/**
 * 组件属性
 */
interface ExecutionCardLoaderProps {
  /** 执行记录 ID */
  executionId: string
  /** 默认展开 */
  defaultExpanded?: boolean
  /** 自定义类名 */
  className?: string
}

/**
 * 加载骨架屏
 */
const LoadingSkeleton: FC<{ className?: string }> = ({ className }) => (
  <div
    className={cn(
      'border-border/50 bg-muted/20 mt-2 animate-pulse rounded-xl border p-3',
      className,
    )}
  >
    <div className="flex items-center gap-2">
      <div className="bg-muted h-7 w-7 rounded-lg" />
      <div className="flex-1 space-y-2">
        <div className="bg-muted h-4 w-1/3 rounded" />
        <div className="bg-muted h-3 w-1/4 rounded" />
      </div>
    </div>
  </div>
)

/**
 * 错误显示
 */
const ErrorDisplay: FC<{ message: string; className?: string }> = ({ message, className }) => (
  <div
    className={cn(
      'mt-2 rounded-xl border border-[var(--badge-error-text)]/30 bg-[var(--badge-error-bg)]',
      'flex items-center gap-2 p-3 text-sm text-[var(--badge-error-text)]',
      className,
    )}
  >
    <AlertCircle className="h-4 w-4 flex-shrink-0" />
    <span>{message}</span>
  </div>
)

/**
 * 执行卡片加载器
 */
const ExecutionCardLoader: FC<ExecutionCardLoaderProps> = ({
  executionId,
  defaultExpanded = false,
  className,
}) => {
  const { activity, loading, error } = useExecutionRecord(executionId)

  if (loading) {
    return <LoadingSkeleton className={className} />
  }

  if (error) {
    return <ErrorDisplay message={`加载执行记录失败: ${error}`} className={className} />
  }

  if (!activity) {
    return (
      <ErrorDisplay
        message={`执行记录不存在: ${executionId.slice(0, 8)}...`}
        className={className}
      />
    )
  }

  return (
    <ActivityCard activity={activity} defaultExpanded={defaultExpanded} className={className} />
  )
}

export default ExecutionCardLoader
