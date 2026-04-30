/**
 * 表单交互组件
 *
 * 根据 Schema 渲染动态表单，支持多种字段类型
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface FormWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 表单交互组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 表单交互的占位渲染
 */
export function FormWidget({ props }: FormWidgetProps) {
  return (
    <div className="space-y-2 rounded-lg border p-4">
      <div className="text-muted-foreground text-sm font-medium">[Form Widget]</div>
      <div className="text-muted-foreground text-xs">表单交互组件 - 待后续 Phase 完善</div>
      {props && (
        <pre className="bg-muted/50 overflow-auto rounded p-2 text-xs">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
