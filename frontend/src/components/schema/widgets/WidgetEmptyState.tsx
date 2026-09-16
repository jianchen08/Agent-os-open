/**
 * 数据组件空态占位（图表/表格共用）：加载中显示状态灯，空闲显示图标 + 文案。
 * 曾在 ChartWidget/TableWidget 各自复制（jscpd 克隆门禁）。
 */
import React from 'react'
import { DataWidgetStatus } from '@/services/schema/dataWidget'

export function WidgetEmptyState({
  loading,
  dashed = false,
  iconInner,
  text,
}: {
  loading: boolean
  dashed?: boolean
  iconInner: React.ReactNode
  text: string
}) {
  return (
    <div
      className={`flex flex-col items-center justify-center rounded-lg border p-8${
        dashed ? ' border-dashed' : ''
      }`}
    >
      <DataWidgetStatus loading={loading} error={null} />
      {!loading && (
        <>
          <svg
            className="text-muted-foreground mb-2 h-12 w-12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            {iconInner}
          </svg>
          <p className="text-muted-foreground text-sm">{text}</p>
        </>
      )}
    </div>
  )
}
