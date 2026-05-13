/**
 * 聊天空间渲染器
 *
 * 负责渲染 chat 空间的渲染指令。
 * 每个指令对应一个聊天交互组件（form/chart/gallery 等）。
 *
 * @module ChatSpaceRenderer
 */

import React from 'react'
import type { RenderInstruction } from '@/services/schema/RenderingEngine'

/** ChatSpaceRenderer 属性 */
export interface ChatSpaceRendererProps {
  /** 渲染指令列表 */
  instructions: RenderInstruction[]
  /** 模块 ID（用于作用域隔离） */
  moduleId?: string
}

/**
 * 聊天空间渲染器
 *
 * 渲染聊天空间中的交互组件列表。
 * 每个组件按指令顺序排列，支持 form/chart/gallery/table 等类型。
 *
 * @param props - 渲染器属性
 * @returns 聊天空间组件列表
 */
export function ChatSpaceRenderer({ instructions }: ChatSpaceRendererProps) {
  if (instructions.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      {instructions.map((instruction, index) => {
        const { component: WidgetComponent, widgetType, props, moduleId } = instruction
        const stableKey = `${moduleId}::chat::${widgetType}::${index}`

        if (!WidgetComponent) {
          return (
            <div
              key={stableKey}
              className="text-muted-foreground rounded-md border border-dashed p-3 text-sm"
            >
              未注册的组件类型: {widgetType}
            </div>
          )
        }

        return (
          <div key={stableKey} className="chat-widget-container">
            <WidgetComponent {...props} />
          </div>
        )
      })}
    </div>
  )
}
