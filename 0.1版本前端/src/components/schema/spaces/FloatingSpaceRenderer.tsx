/**
 * 悬浮窗空间渲染器
 *
 * 负责渲染 floating 空间的渲染指令。
 * 悬浮窗为可拖拽、可调整大小的浮动容器。
 *
 * @module FloatingSpaceRenderer
 */

import React from 'react'
import type { RenderInstruction } from '@/services/schema/RenderingEngine'

/** FloatingSpaceRenderer 属性 */
export interface FloatingSpaceRendererProps {
  /** 渲染指令列表 */
  instructions: RenderInstruction[]
  /** 模块 ID */
  moduleId?: string
}

/**
 * 悬浮窗空间渲染器
 *
 * 渲染浮动面板列表，每个面板承载一个悬浮窗组件。
 * 支持自定义位置、大小和自动弹出。
 *
 * @param props - 渲染器属性
 * @returns 悬浮窗面板列表
 */
export function FloatingSpaceRenderer({ instructions }: FloatingSpaceRendererProps) {
  if (instructions.length === 0) {
    return null
  }

  return (
    <div className="floating-space-container">
      {instructions.map((instruction) => {
        const {
          component: WidgetComponent,
          moduleId,
          widgetType,
          props,
          layout,
        } = instruction
        const stableKey = `${moduleId}::floating::${widgetType}`

        // 布局样式
        const containerStyle: React.CSSProperties = {
          width: layout?.width ?? 320,
          minHeight: layout?.minHeight ?? 200,
        }

        return (
          <div
            key={stableKey}
            className="bg-background text-foreground border-border shadow-lg fixed rounded-lg border"
            style={{
              ...containerStyle,
              ...(layout?.position === 'bottom-right'
                ? { bottom: 80, right: 16 }
                : layout?.position === 'bottom-left'
                  ? { bottom: 80, left: 16 }
                  : layout?.position === 'top-right'
                    ? { top: 16, right: 16 }
                    : layout?.position === 'top-left'
                      ? { top: 16, left: 16 }
                      : { bottom: 80, right: 16 }),
              zIndex: 50,
            }}
          >
            {/* 标题栏 */}
            <div className="border-border flex items-center justify-between border-b px-3 py-2">
              <span className="text-sm font-medium">
                {props.title ?? widgetType}
              </span>
            </div>

            {/* 内容区 */}
            <div className="overflow-auto p-3">
              {!WidgetComponent ? (
                <div className="text-muted-foreground p-4 text-center text-sm">
                  未注册的组件类型: {widgetType}
                </div>
              ) : (
                <WidgetComponent {...props} />
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
