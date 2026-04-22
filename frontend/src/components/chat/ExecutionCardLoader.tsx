/**
 * 执行卡片加载器组件
 *
 * 根据执行记录 ID 查询数据并渲染活动卡片
 */

import { useExecutionRecord } from '@/hooks/useExecutionRecord'
import { cn } from '@/lib/utils'
import { AlertCircle } from 'lucide-react'
import type { FC } from 'react'
import ActivityCard from './ActivityCard'

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
      'mt-2 rounded-xl border border-border/50 bg-muted/20 p-3 animate-pulse',
      className
    )}
  >
    <div className="flex items-center gap-2">
      <div className="w-7 h-7 rounded-lg bg-muted" />
      <div className="flex-1 space-y-2">
        <div className="h-4 bg-muted rounded w-1/3" />
        <div className="h-3 bg-muted rounded w-1/4" />
      </div>
    </div>
  </div>
)

/**
 * 错误显示
 */
const ErrorDisplay: FC<{ message: string; className?: string }> = ({
  message,
  className,
}) => (
  <div
    className={cn(
      'mt-2 rounded-xl border border-red-200/50 bg-red-50/50',
      'dark:border-red-800/30 dark:bg-red-900/10',
      'p-3 flex items-center gap-2 text-sm text-red-600 dark:text-red-400',
      className
    )}
  >
    <AlertCircle className="w-4 h-4 flex-shrink-0" />
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
    <ActivityCard
      activity={activity}
      defaultExpanded={defaultExpanded}
      className={className}
    />
  )
}

export default ExecutionCardLoader
