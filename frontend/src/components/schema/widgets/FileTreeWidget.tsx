/**
 * 文件树组件
 *
 * 根据 Schema 渲染文件树结构，支持目录浏览和文件操作
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface FileTreeWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 文件树组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 文件树组件的占位渲染
 */
export function FileTreeWidget({ props }: FileTreeWidgetProps) {
  return (
    <div className="space-y-2 rounded-lg border p-4">
      <div className="text-muted-foreground text-sm font-medium">[FileTree Widget]</div>
      <div className="text-muted-foreground text-xs">文件树组件 - 待后续 Phase 完善</div>
      {props && (
        <pre className="bg-muted/50 overflow-auto rounded p-2 text-xs">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
