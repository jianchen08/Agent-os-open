/**
 * 编辑器组件
 *
 * 根据 Schema 渲染代码编辑器，将来集成 Monaco Editor
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface EditorWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 编辑器组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 编辑器组件的占位渲染
 */
export function EditorWidget({ props }: EditorWidgetProps) {
  return (
    <div className="rounded-lg border p-4 space-y-2">
      <div className="text-sm font-medium text-muted-foreground">[Editor Widget]</div>
      <div className="text-xs text-muted-foreground">
        编辑器组件 - 将来集成 Monaco Editor
      </div>
      {props && (
        <pre className="text-xs bg-muted/50 rounded p-2 overflow-auto">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
