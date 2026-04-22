/**
 * 状态卡片组件
 *
 * 根据 Schema 渲染状态信息卡片，支持状态指示和操作按钮
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface StatusCardWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 状态卡片组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 状态卡片的占位渲染
 */
export function StatusCardWidget({ props }: StatusCardWidgetProps) {
  return (
    <div className="rounded-lg border p-4 space-y-2">
      <div className="text-sm font-medium text-muted-foreground">[StatusCard Widget]</div>
      <div className="text-xs text-muted-foreground">
        状态卡片组件 - 待后续 Phase 完善
      </div>
      {props && (
        <pre className="text-xs bg-muted/50 rounded p-2 overflow-auto">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
