/**
 * 工作区空间渲染器
 *
 * 负责渲染 workspace 空间的渲染指令。
 * 工作区面板承载复杂工具的完整 UI，支持 Tab 切换。
 *
 * @module WorkspaceSpaceRenderer
 */

import React, { useState } from 'react'
import type { RenderInstruction } from '@/services/schema/RenderingEngine'

/** WorkspaceSpaceRenderer 属性 */
export interface WorkspaceSpaceRendererProps {
  /** 渲染指令列表 */
  instructions: RenderInstruction[]
  /** 模块 ID */
  moduleId?: string
}

/**
 * 工作区空间渲染器
 *
 * 以 Tab 形式展示多个工作区组件，每个 Tab 对应一个渲染指令。
 * 支持组件切换，默认激活第一个 Tab。
 *
 * @param props - 渲染器属性
 * @returns 工作区面板组件
 */
export function WorkspaceSpaceRenderer({
  instructions,
}: WorkspaceSpaceRendererProps) {
  const [activeIndex, setActiveIndex] = useState(0)

  if (instructions.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-muted-foreground text-sm">暂无工作区内容</p>
      </div>
    )
  }

  const activeInstruction = instructions[activeIndex]
  const { component: WidgetComponent, moduleId, widgetType, props } = activeInstruction

  return (
    <div className="flex h-full flex-col">
      {/* Tab 栏 */}
      {instructions.length > 1 && (
        <div className="border-border flex border-b">
          {instructions.map((inst, i) => (
            <button
              key={`${inst.moduleId}::workspace::${inst.widgetType}::${i}`}
              type="button"
              className={`cursor-pointer border-b-2 px-4 py-2 text-sm transition-colors ${
                i === activeIndex
                  ? 'border-primary text-foreground font-medium'
                  : 'text-muted-foreground border-transparent hover:text-foreground'
              }`}
              onClick={() => setActiveIndex(i)}
            >
              {inst.props.title ?? inst.widgetType}
            </button>
          ))}
        </div>
      )}

      {/* 渲染区域 */}
      <div className="flex-1 overflow-auto p-2">
        {!WidgetComponent ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-muted-foreground text-sm">
              未注册的组件类型: {widgetType}
            </p>
          </div>
        ) : (
          <WidgetComponent {...props} />
        )}
      </div>
    </div>
  )
}
