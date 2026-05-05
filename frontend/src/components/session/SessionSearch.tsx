/**
 * 会话搜索组件
 *
 * 提供侧边栏中的会话搜索功能，支持实时过滤。
 * 当有搜索关键词时，显示匹配结果数量。
 */

import { Search, X } from 'lucide-react'
import { memo, useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'

interface SessionSearchProps {
  /** 搜索关键词变更回调 */
  onSearchChange: (keyword: string) => void
  /** 当前匹配结果数量 */
  resultCount: number
  /** 总会话数量 */
  totalCount: number
  /** 自定义容器类名 */
  className?: string
  /** 自定义输入框类名 */
  inputClassName?: string
}

/**
 * 会话搜索组件
 * 支持实时搜索和清除，显示搜索结果计数
 */
export const SessionSearch = memo<SessionSearchProps>(
  ({ onSearchChange, resultCount, totalCount, className, inputClassName }) => {
    const inputRef = useRef<HTMLInputElement>(null)

    /**
     * 处理输入变更，通知父组件更新搜索关键词
     */
    const handleInputChange = useCallback(
      (e: React.ChangeEvent<HTMLInputElement>) => {
        onSearchChange(e.target.value)
      },
      [onSearchChange],
    )

    /**
     * 清除搜索关键词并聚焦输入框
     */
    const handleClear = useCallback(() => {
      onSearchChange('')
      inputRef.current?.focus()
    }, [onSearchChange])

    return (
      <div className={cn('relative', className)}>
        <Search className="text-muted-foreground absolute top-1/2 left-2 h-3.5 w-3.5 -translate-y-1/2" />
        <input
          ref={inputRef}
          type="text"
          placeholder="搜索会话..."
          onChange={handleInputChange}
          className={cn(
            'bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border py-1 pr-7 pl-7 text-xs outline-none transition-colors',
            inputClassName,
          )}
          aria-label="搜索会话"
        />
        {resultCount < totalCount && (
          <span className="text-muted-foreground absolute top-1/2 right-6 -translate-y-1/2 text-[10px]">
            {resultCount}/{totalCount}
          </span>
        )}
        <button
          onClick={handleClear}
          className="text-muted-foreground hover:text-foreground absolute top-1/2 right-1.5 -translate-y-1/2 rounded p-0.5"
          aria-label="清除搜索"
        >
          <X className="h-3 w-3" />
        </button>
      </div>
    )
  },
)

SessionSearch.displayName = 'SessionSearch'
