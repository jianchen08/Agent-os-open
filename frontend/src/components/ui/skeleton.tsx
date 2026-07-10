import { cn } from '@/lib/utils'

/**
 * 骨架屏组件
 * 用于内容加载时的占位显示
 *
 * @example
 * // 文本骨架
 * <Skeleton className="h-4 w-[200px]" />
 *
 * // 头像骨架
 * <Skeleton className="h-12 w-12 rounded-full" />
 *
 * // 卡片骨架
 * <div className="space-y-2">
 *   <Skeleton className="h-4 w-[250px]" />
 *   <Skeleton className="h-4 w-[200px]" />
 * </div>
 */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('bg-primary/10 animate-pulse rounded-md', className)} {...props} />
}

export { Skeleton }
