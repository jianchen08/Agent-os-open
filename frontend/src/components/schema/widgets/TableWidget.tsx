/**
 * 数据表格组件
 *
 * 根据 Schema 渲染数据表格，支持列排序、分页、斑马纹和 hover 高亮。
 *
 * @module TableWidget
 */

import React, { useState, useMemo, useCallback } from 'react'

/** 列定义 */
interface ColumnDef {
  /** 列标识 */
  key: string
  /** 列标题 */
  label: string
  /** 是否可排序 */
  sortable?: boolean
  /** 列宽度 */
  width?: number | string
}

/** 排序方向 */
type SortDirection = 'asc' | 'desc' | null

/** 排序状态 */
interface SortState {
  /** 排序列 key */
  key: string
  /** 排序方向 */
  direction: SortDirection
}

/**
 * 提取列定义
 *
 * @param columns - 原始列定义
 * @returns 类型安全的 ColumnDef 数组
 */
function extractColumns(columns: unknown): ColumnDef[] {
  if (!Array.isArray(columns)) return []
  return columns.filter(
    (col): col is ColumnDef =>
      typeof col === 'object' && col !== null && typeof (col as ColumnDef).key === 'string',
  )
}

/**
 * 提取行数据
 *
 * @param data - 原始行数据
 * @returns 类型安全的行数据数组
 */
function extractRows(data: unknown): Record<string, unknown>[] {
  if (!Array.isArray(data)) return []
  return data.filter(
    (row): row is Record<string, unknown> => typeof row === 'object' && row !== null,
  )
}

/**
 * 数据表格组件
 *
 * 支持列排序（点击表头切换升序/降序）、分页、斑马纹和 hover 高亮。
 *
 * @param props - 组件属性，包含 columns、data、pageSize 等
 * @returns 表格渲染结果
 */
export function TableWidget(props: Record<string, unknown>) {
  const columns = extractColumns(props.columns)
  const rows = extractRows(props.data)
  const pageSize = (props.pageSize as number) ?? 10
  const title = props.title as string | undefined

  const [sortState, setSortState] = useState<SortState | null>(null)
  const [currentPage, setCurrentPage] = useState(1)

  const handleSort = useCallback(
    (key: string) => {
      setSortState((prev) => {
        if (!prev || prev.key !== key) {
          return { key, direction: 'asc' }
        }
        if (prev.direction === 'asc') {
          return { key, direction: 'desc' }
        }
        return null
      })
      setCurrentPage(1)
    },
    [],
  )

  const sortedRows = useMemo(() => {
    if (!sortState) return rows
    const { key, direction } = sortState
    return [...rows].sort((a, b) => {
      const va = a[key]
      const vb = b[key]
      if (va === vb) return 0
      if (va === undefined || va === null) return 1
      if (vb === undefined || vb === null) return -1

      let cmp = 0
      if (typeof va === 'number' && typeof vb === 'number') {
        cmp = va - vb
      } else {
        cmp = String(va).localeCompare(String(vb))
      }
      return direction === 'desc' ? -cmp : cmp
    })
  }, [rows, sortState])

  const totalPages = Math.max(1, Math.ceil(sortedRows.length / pageSize))
  const safeCurrentPage = Math.min(currentPage, totalPages)

  const pagedRows = useMemo(() => {
    const start = (safeCurrentPage - 1) * pageSize
    return sortedRows.slice(start, start + pageSize)
  }, [sortedRows, safeCurrentPage, pageSize])

  if (columns.length === 0 && rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border border-dashed p-8">
        <svg
          className="text-muted-foreground mb-2 h-12 w-12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <path d="M3 9h18M3 15h18M9 3v18M15 3v18" />
        </svg>
        <p className="text-muted-foreground text-sm">暂无表格数据</p>
      </div>
    )
  }

  if (columns.length === 0) {
    return (
      <div className="text-muted-foreground rounded-lg border p-4 text-center text-sm">
        未定义列
      </div>
    )
  }

  return (
    <div className="w-full overflow-hidden rounded-lg border">
      {title && (
        <div className="border-b bg-muted/50 px-4 py-2">
          <h3 className="text-foreground text-sm font-semibold">{title}</h3>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b bg-muted/30">
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={`text-muted-foreground px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider ${
                    col.sortable ? 'cursor-pointer select-none hover:text-foreground' : ''
                  }`}
                  style={col.width ? { width: col.width } : undefined}
                  onClick={col.sortable ? () => handleSort(col.key) : undefined}
                >
                  <span className="inline-flex items-center gap-1">
                    {col.label}
                    {col.sortable && (
                      <span className="inline-flex flex-col">
                        <span
                          className={`leading-none text-[10px] ${
                            sortState?.key === col.key && sortState.direction === 'asc'
                              ? 'text-foreground'
                              : 'text-muted-foreground/40'
                          }`}
                        >
                          ▲
                        </span>
                        <span
                          className={`leading-none text-[10px] ${
                            sortState?.key === col.key && sortState.direction === 'desc'
                              ? 'text-foreground'
                              : 'text-muted-foreground/40'
                          }`}
                        >
                          ▼
                        </span>
                      </span>
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pagedRows.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="text-muted-foreground px-4 py-8 text-center"
                >
                  无数据
                </td>
              </tr>
            ) : (
              pagedRows.map((row, ri) => (
                <tr
                  key={ri}
                  className="border-b transition-colors last:border-b-0 hover:bg-muted/30"
                  style={ri % 2 === 1 ? { backgroundColor: 'var(--muted, #f9fafb)' } : undefined}
                >
                  {columns.map((col) => (
                    <td key={col.key} className="px-4 py-2.5 text-sm">
                      {renderCellValue(row[col.key])}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="border-t bg-muted/20 flex items-center justify-between px-4 py-2">
          <span className="text-muted-foreground text-xs">
            共 {sortedRows.length} 条，第 {safeCurrentPage}/{totalPages} 页
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setCurrentPage(1)}
              disabled={safeCurrentPage <= 1}
              className="text-muted-foreground hover:text-foreground rounded px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
            >
              首页
            </button>
            <button
              onClick={() => setCurrentPage(Math.max(1, safeCurrentPage - 1))}
              disabled={safeCurrentPage <= 1}
              className="text-muted-foreground hover:text-foreground rounded px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
            >
              ‹ 上一页
            </button>
            <button
              onClick={() => setCurrentPage(Math.min(totalPages, safeCurrentPage + 1))}
              disabled={safeCurrentPage >= totalPages}
              className="text-muted-foreground hover:text-foreground rounded px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
            >
              下一页 ›
            </button>
            <button
              onClick={() => setCurrentPage(totalPages)}
              disabled={safeCurrentPage >= totalPages}
              className="text-muted-foreground hover:text-foreground rounded px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
            >
              末页
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * 渲染单元格值
 *
 * @param value - 单元格原始值
 * @returns 格式化后的显示内容
 */
function renderCellValue(value: unknown): React.ReactNode {
  if (value === undefined || value === null) return '—'
  if (typeof value === 'boolean') return value ? '✓' : '✗'
  if (typeof value === 'object') {
    try {
      return <span className="font-mono text-xs">{JSON.stringify(value)}</span>
    } catch {
      return String(value)
    }
  }
  return String(value)
}
