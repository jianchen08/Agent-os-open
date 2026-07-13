/**
 * LoadingState 加载状态
 *
 * 支持两种变体：spinner（旋转圈 + 文字）和 skeleton（骨架屏卡片）。
 * spinner 模式参考 AdminPage 的加载样式，skeleton 模式参考 AgentsPage 的骨架屏。
 */

/** LoadingState 组件属性 */
interface LoadingStateProps {
  /** 加载变体：spinner（旋转圈）或 skeleton（骨架屏卡片），默认 'spinner' */
  variant?: 'spinner' | 'skeleton'
  /** 加载提示文字，默认 '加载中...' */
  text?: string
  /** 骨架屏卡片数量，默认 6 */
  skeletonCount?: number
}

/**
 * 旋转加载指示器
 */
function SpinnerIndicator({ text }: { text: string }) {
  return (
    <div className="flex items-center justify-center py-12">
      <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
      <span className="text-muted-foreground ml-2 text-sm">{text}</span>
    </div>
  )
}

/**
 * 骨架屏卡片
 */
function SkeletonCard() {
  return (
    <div className="animate-pulse rounded-lg border p-4">
      <div className="mb-2 flex items-start justify-between">
        <div className="bg-muted h-4 w-2/3 rounded" />
        <div className="bg-muted h-5 w-12 rounded-full" />
      </div>
      <div className="bg-muted mb-3 h-3 w-full rounded" />
      <div className="bg-muted mb-1.5 h-3 w-4/5 rounded" />
      <div className="flex gap-1.5">
        <div className="bg-muted h-5 w-10 rounded" />
        <div className="bg-muted h-5 w-16 rounded" />
      </div>
    </div>
  )
}

/**
 * 加载状态组件
 *
 * - variant='spinner'：居中展示旋转圆圈 + 文字提示
 * - variant='skeleton'：网格排列的骨架屏卡片，适用于列表页加载
 */
export function LoadingState({
  variant = 'spinner',
  text = '加载中...',
  skeletonCount = 6,
}: LoadingStateProps) {
  if (variant === 'spinner') {
    return <SpinnerIndicator text={text} />
  }

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: skeletonCount }).map((_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  )
}
