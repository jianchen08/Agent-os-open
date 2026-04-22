/**
 * 看板组件
 *
 * 根据 Schema 渲染看板视图，支持拖拽卡片和列管理
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface KanbanWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 看板组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 看板组件的占位渲染
 */
export function KanbanWidget({ props }: KanbanWidgetProps) {
  return (
    <div className="rounded-lg border p-4 space-y-2">
      <div className="text-sm font-medium text-muted-foreground">[Kanban Widget]</div>
      <div className="text-xs text-muted-foreground">
        看板组件 - 待后续 Phase 完善
      </div>
      {props && (
        <pre className="text-xs bg-muted/50 rounded p-2 overflow-auto">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
