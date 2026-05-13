/**
 * Dock 栏空间渲染器
 *
 * 负责渲染 dock 空间的渲染指令。
 * Dock 栏为底部快捷入口，包含图标、标签和状态指示灯。
 *
 * @module DockSpaceRenderer
 */

import React from 'react'
import type { RenderInstruction } from '@/services/schema/RenderingEngine'

/** DockSpaceRenderer 属性 */
export interface DockSpaceRendererProps {
  /** 渲染指令列表 */
  instructions: RenderInstruction[]
  /** 点击回调（点击 Dock 入口时触发） */
  onItemClick?: (moduleId: string) => void
}

/**
 * Dock 栏空间渲染器
 *
 * 渲染底部 Dock 栏的模块入口列表。
 * 每个入口显示图标、标签和可选的状态指示灯。
 *
 * @param props - 渲染器属性
 * @returns Dock 栏组件
 */
export function DockSpaceRenderer({
  instructions,
  onItemClick,
}: DockSpaceRendererProps) {
  if (instructions.length === 0) {
    return null
  }

  return (
    <div className="border-border bg-background/95 flex items-center gap-1 border-t px-2 py-1 backdrop-blur-sm">
      {instructions.map((instruction) => {
        const { moduleId, widgetType, props } = instruction
        const stableKey = `${moduleId}::dock::${widgetType}`
        const icon = (props.icon as string) ?? '📦'
        const label = (props.label as string) ?? moduleId
        const indicator = (props.indicator as string) ?? 'none'
        const indicatorColor = (props.indicatorColor as string) ?? 'bg-status-success'

        return (
          <button
            key={stableKey}
            type="button"
            className="hover:bg-muted relative flex flex-col items-center rounded-md px-3 py-1.5 text-xs transition-colors"
            title={label}
            onClick={() => onItemClick?.(moduleId)}
          >
            {/* 图标 */}
            <span className="text-base">{icon}</span>

            {/* 标签 */}
            <span className="text-muted-foreground mt-0.5 max-w-[60px] truncate">
              {label}
            </span>

            {/* 状态指示灯 */}
            {indicator === 'dot' && (
              <span
                className={`absolute top-1 right-1 h-1.5 w-1.5 rounded-full ${indicatorColor}`}
              />
            )}
            {indicator === 'badge' && (
              <span
                className={`absolute -top-0.5 -right-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full text-[8px] text-white ${indicatorColor}`}
              >
                !
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
