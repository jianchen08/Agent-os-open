/**
 * ErrorState 错误提示
 *
 * 统一的错误展示组件，支持 inline（横幅）和 center（居中大图标）两种变体。
 * 可选的重试按钮用于触发数据重新加载。
 */

import { AlertTriangle, RefreshCw } from 'lucide-react'

/** ErrorState 组件属性 */
interface ErrorStateProps {
  /** 错误信息文本 */
  message: string
  /** 重试回调，不传则不显示重试按钮 */
  onRetry?: () => void
  /** 展示变体：inline（横幅式）或 center（居中图标式），默认 'inline' */
  variant?: 'inline' | 'center'
}

/**
 * 内联横幅式错误提示
 */
function InlineError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="bg-destructive/10 text-destructive flex items-center gap-3 rounded-lg p-4 text-sm">
      <span className="flex-1">{message}</span>
      {onRetry && (
        <button
          onClick={onRetry}
          className="hover:bg-destructive/20 flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors"
          aria-label="重试"
        >
          <RefreshCw className="h-3 w-3" />
          重试
        </button>
      )}
    </div>
  )
}

/**
 * 居中图标式错误提示
 */
function CenterError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16">
      <AlertTriangle className="text-destructive mb-3 h-12 w-12" />
      <p className="text-destructive text-sm">{message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="hover:bg-accent/50 mt-4 flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition-colors"
          aria-label="重试"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重试
        </button>
      )}
    </div>
  )
}

/**
 * 错误提示组件
 *
 * - variant='inline'：横幅式错误提示，带背景色和可选重试按钮，适合列表页顶部
 * - variant='center'：居中图标式错误提示，适合整页错误状态
 */
export function ErrorState({ message, onRetry, variant = 'inline' }: ErrorStateProps) {
  if (variant === 'center') {
    return <CenterError message={message} onRetry={onRetry} />
  }
  return <InlineError message={message} onRetry={onRetry} />
}
