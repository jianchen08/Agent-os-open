/**
 * 全屏覆盖层组件
 *
 * 支持进入/退出全屏模式，按 Schema 渲染内容
 */

import React from 'react'

/** 全屏覆盖层属性 */
interface FullscreenOverlayProps {
  /** 是否激活全屏模式 */
  isActive: boolean
  /** 全屏标题 */
  title?: string
  /** 退出全屏回调 */
  onExit: () => void
  /** 全屏内容 */
  children?: React.ReactNode
}

/**
 * 全屏覆盖层组件
 *
 * 当 isActive 为 true 时渲染全屏覆盖层，包含顶部工具栏和内容区域
 */
export function FullscreenOverlay({ isActive, title, onExit, children }: FullscreenOverlayProps) {
  if (!isActive) return null

  return (
    <div className="fixed inset-0 bg-background z-[100] flex flex-col">
      {/* 顶部工具栏 */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border">
        <span className="text-sm font-medium text-foreground">{title ?? '全屏模式'}</span>
        <button
          className="px-3 py-1 text-sm rounded-md hover:bg-accent text-muted-foreground"
          onClick={onExit}
        >
          退出全屏 (Esc)
        </button>
      </div>

      {/* 全屏内容 */}
      <div className="flex-1 overflow-auto">
        {children}
      </div>
    </div>
  )
}
