/**
 * 会话搜索组件（P2 搜索框合并）
 *
 * 提供侧边栏统一搜索输入框：支持搜索类型切换（全部/会话/消息），
 * 输入由父组件（Sidebar）防抖调用后端搜索 API，本组件为受控展示。
 */

import { Search, X } from '@/assets/icons'
import { memo, useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'
import type { SearchType } from '@/services/api/search'

interface SessionSearchProps {
  /** 搜索关键词（受控） */
  value: string
  /** 搜索关键词变更回调 */
  onSearchChange: (keyword: string) => void
  /** 搜索类型（受控） */
  searchType: SearchType
  /** 搜索类型变更回调 */
  onSearchTypeChange: (type: SearchType) => void
  /** 当前匹配结果数量（会话） */
  resultCount: number
  /** 总会话数量 */
  totalCount: number
  /** 是否正在请求后端搜索 */
  isSearching?: boolean
  /** 自定义容器类名 */
  className?: string
  /** 自定义输入框类名 */
  inputClassName?: string
}

/** 搜索类型选项 */
const TYPE_OPTIONS: Array<{ value: SearchType; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'session', label: '会话' },
  { value: 'message', label: '消息' },
]

/**
 * 会话搜索组件（P2 合并版）
 * 支持搜索类型切换，输入受控，清除后聚焦。
 */
export const SessionSearch = memo<SessionSearchProps>(
  ({
    value,
    onSearchChange,
    searchType,
    onSearchTypeChange,
    resultCount,
    totalCount,
    isSearching = false,
    className,
    inputClassName,
  }) => {
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
        {/* 搜索类型切换（Tab） */}
        <div className="mb-1 flex gap-0.5">
          {TYPE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => onSearchTypeChange(opt.value)}
              className={cn(
                'rounded px-2 py-0.5 text-[11px] transition-colors',
                searchType === opt.value
                  ? 'bg-primary/10 text-primary font-medium'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted/50',
              )}
              data-testid={`search-type-${opt.value}`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <div className="relative">
          <Search className="text-muted-foreground absolute top-1/2 left-2 h-3.5 w-3.5 -translate-y-1/2" />
          <input
            ref={inputRef}
            type="text"
            value={value}
            placeholder={searchType === 'message' ? '搜索消息内容...' : '搜索会话...'}
            onChange={handleInputChange}
            className={cn(
              'bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border py-1 pr-7 pl-7 text-xs outline-none transition-colors',
              inputClassName,
            )}
            aria-label="搜索"
          />
          {isSearching ? (
            <span className="text-muted-foreground absolute top-1/2 right-2 -translate-y-1/2 text-[10px]">
              搜索中...
            </span>
          ) : (
            value &&
            resultCount < totalCount && (
              <span className="text-muted-foreground absolute top-1/2 right-6 -translate-y-1/2 text-[10px]">
                {resultCount}/{totalCount}
              </span>
            )
          )}
          {value && (
            <button
              onClick={handleClear}
              className="text-muted-foreground hover:text-foreground absolute top-1/2 right-1.5 -translate-y-1/2 rounded p-0.5"
              aria-label="清除搜索"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
    )
  },
)

SessionSearch.displayName = 'SessionSearch'
