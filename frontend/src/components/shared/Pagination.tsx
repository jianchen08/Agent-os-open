/**
 * Pagination 分页
 *
 * 简洁的上一页/下一页分页控件，显示当前页/总页数。
 * 样式与项目现有按钮风格一致。
 */

import { ChevronLeft, ChevronRight } from 'lucide-react'

/** Pagination 组件属性 */
interface PaginationProps {
  /** 当前页码（从 1 开始） */
  current: number
  /** 总条目数 */
  total: number
  /** 每页条数，默认 20 */
  pageSize?: number
  /** 页码变更回调 */
  onChange: (page: number) => void
}

/**
 * 分页组件
 *
 * 显示「← 上一页  当前页/总页数  下一页 →」的简洁分页控件。
 * 按钮在首页/末页自动禁用。
 */
export function Pagination({ current, total, pageSize = 20, onChange }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const hasPrev = current > 1
  const hasNext = current < totalPages

  return (
    <div className="flex items-center justify-center gap-2 py-2">
      <button
        onClick={() => hasPrev && onChange(current - 1)}
        disabled={!hasPrev}
        className="hover:bg-accent/50 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-50"
        aria-label="上一页"
      >
        <ChevronLeft className="inline h-4 w-4 align-middle" />
        <span className="ml-0.5 align-middle">上一页</span>
      </button>
      <span className="text-muted-foreground min-w-[5rem] text-center text-sm">
        {current} / {totalPages}
      </span>
      <button
        onClick={() => hasNext && onChange(current + 1)}
        disabled={!hasNext}
        className="hover:bg-accent/50 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-50"
        aria-label="下一页"
      >
        <span className="mr-0.5 align-middle">下一页</span>
        <ChevronRight className="inline h-4 w-4 align-middle" />
      </button>
    </div>
  )
}
