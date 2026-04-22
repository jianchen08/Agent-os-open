/**
 * 决策选择组件
 *
 * 根据 Schema 渲染决策选项，支持单选/多选决策
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface DecisionWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 决策选择组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 决策选择的占位渲染
 */
export function DecisionWidget({ props }: DecisionWidgetProps) {
  return (
    <div className="rounded-lg border p-4 space-y-2">
      <div className="text-sm font-medium text-muted-foreground">[Decision Widget]</div>
      <div className="text-xs text-muted-foreground">
        决策选择组件 - 待后续 Phase 完善
      </div>
      {props && (
        <pre className="text-xs bg-muted/50 rounded p-2 overflow-auto">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
