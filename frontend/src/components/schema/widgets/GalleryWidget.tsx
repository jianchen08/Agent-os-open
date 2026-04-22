/**
 * 图片画廊组件
 *
 * 根据 Schema 渲染图片画廊，支持网格和轮播模式
 * 当前为 stub 实现，后续 Phase 会完善
 */

import React from 'react'

interface GalleryWidgetProps {
  /** 组件配置 */
  props?: Record<string, unknown>
}

/**
 * 图片画廊组件 Stub
 *
 * @param props - 组件配置属性
 * @returns 图片画廊的占位渲染
 */
export function GalleryWidget({ props }: GalleryWidgetProps) {
  return (
    <div className="rounded-lg border p-4 space-y-2">
      <div className="text-sm font-medium text-muted-foreground">[Gallery Widget]</div>
      <div className="text-xs text-muted-foreground">
        图片画廊组件 - 待后续 Phase 完善
      </div>
      {props && (
        <pre className="text-xs bg-muted/50 rounded p-2 overflow-auto">
          {JSON.stringify(props, null, 2)}
        </pre>
      )}
    </div>
  )
}
