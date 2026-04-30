/**
 * 数据表格组件
 *
 * 根据 Schema 渲染数据表格，支持排序、筛选和分页
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface TableWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 数据表格组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 数据表格的占位渲染
 */
export function TableWidget({ props }: TableWidgetProps) {
  return (
    <div className="space-y-2 rounded-lg border p-4">
      <div className="text-muted-foreground text-sm font-medium">[Table Widget]</div>
      <div className="text-muted-foreground text-xs">数据表格组件 - 待后续 Phase 完善</div>
      {props && (
        <pre className="bg-muted/50 overflow-auto rounded p-2 text-xs">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
