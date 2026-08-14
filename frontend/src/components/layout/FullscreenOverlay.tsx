/**
 * 全屏覆盖层组件
 *
 * 支持进入/退出全屏模式，按 Schema 渲染内容。
 * 顶栏样式与轻顶栏统一（44px、panel 背景，见 task_layout_responsive 任务 4）。
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
    <div className="bg-background text-foreground fixed inset-0 z-[100] flex flex-col">
      {/* 顶部工具栏（与轻顶栏同一高度/背景体系） */}
      <div
        className="border-border flex items-center justify-between border-b px-2 md:px-3"
        style={{
          height: 'var(--layout-titlebar-height, 44px)',
          background: 'var(--ds-bg-panel, hsl(var(--card)))',
        }}
        data-testid="fullscreen-toolbar"
      >
        <span className="text-foreground truncate text-[13px] font-medium">{title ?? '全屏模式'}</span>
        <button
          className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-7 items-center justify-center rounded-md px-2.5 text-xs transition-colors"
          onClick={onExit}
        >
          退出全屏 (Esc)
        </button>
      </div>

      {/* 全屏内容 */}
      <div className="flex-1 overflow-auto">{children}</div>
    </div>
  )
}
