/**
 * 图表展示组件
 *
 * 根据 Schema 渲染各类图表（折线、柱状、饼图等）
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface ChartWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 图表展示组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 图表展示的占位渲染
 */
export function ChartWidget({ props }: ChartWidgetProps) {
  return (
    <div className="space-y-2 rounded-lg border p-4">
      <div className="text-muted-foreground text-sm font-medium">[Chart Widget]</div>
      <div className="text-muted-foreground text-xs">图表展示组件 - 待后续 Phase 完善</div>
      {props && (
        <pre className="bg-muted/50 overflow-auto rounded p-2 text-xs">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
