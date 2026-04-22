/**
 * 悬浮窗管理器
 *
 * 管理多个悬浮窗实例，支持拖拽、调整大小和 z-index 层级管理
 */

import React, { useState, useCallback, useRef } from 'react'
import type { FloatingWindowInstance } from '@/types/layout'

/** 悬浮窗管理器属性 */
interface FloatingWindowManagerProps {
  /** 悬浮窗实例列表 */
  windows: FloatingWindowInstance[]
  /** 更新悬浮窗属性回调 */
  onUpdateWindow: (id: string, updates: Partial<FloatingWindowInstance>) => void
  /** 关闭悬浮窗回调 */
  onCloseWindow: (id: string) => void
  /** 渲染悬浮窗内容的函数 */
  renderContent: (window: FloatingWindowInstance) => React.ReactNode
}

/**
 * 悬浮窗管理器组件
 *
 * 渲染所有悬浮窗实例，处理拖拽移动和最小化/关闭操作
 */
export function FloatingWindowManager({ windows, onUpdateWindow, onCloseWindow, renderContent }: FloatingWindowManagerProps) {
  const [dragState, setDragState] = useState<{ windowId: string; startX: number; startY: number; startPosX: number; startPosY: number } | null>(null)

  /**
   * 处理悬浮窗拖拽开始
   *
   * 记录起始位置，注册全局 mousemove/mouseup 事件
   */
  const handleDragStart = useCallback((windowId: string, e: React.MouseEvent) => {
    const win = windows.find(w => w.id === windowId)
    if (!win) return

    setDragState({
      windowId,
      startX: e.clientX,
      startY: e.clientY,
      startPosX: win.position.x,
      startPosY: win.position.y,
    })

    const handleMove = (moveEvent: MouseEvent) => {
      const dx = moveEvent.clientX - e.clientX
      const dy = moveEvent.clientY - e.clientY
      onUpdateWindow(windowId, {
        position: { x: win.position.x + dx, y: win.position.y + dy },
      })
    }

    const handleUp = () => {
      document.removeEventListener('mousemove', handleMove)
      document.removeEventListener('mouseup', handleUp)
      setDragState(null)
    }

    document.addEventListener('mousemove', handleMove)
    document.addEventListener('mouseup', handleUp)
  }, [windows, onUpdateWindow])

  if (windows.length === 0) return null

  return (
    <>
      {windows.map(win => (
        <div
          key={win.id}
          className="fixed pointer-events-auto bg-background border border-border rounded-lg shadow-lg overflow-hidden"
          style={{
            left: win.position.x,
            top: win.position.y,
            width: win.size.width,
            height: win.isMinimized ? 36 : win.size.height,
            zIndex: win.zIndex,
            transition: dragState?.windowId === win.id ? 'none' : 'all 0.2s ease',
          }}
        >
          {/* 标题栏 */}
          <div
            className="flex items-center justify-between px-3 py-2 bg-muted/50 cursor-move border-b border-border"
            onMouseDown={e => handleDragStart(win.id, e)}
          >
            <span className="text-sm font-medium text-foreground truncate">
              {win.icon && <span className="mr-2">{win.icon}</span>}
              {win.title}
            </span>
            <div className="flex items-center gap-1">
              <button
                className="w-5 h-5 flex items-center justify-center rounded hover:bg-accent text-muted-foreground text-xs"
                onClick={() => onUpdateWindow(win.id, { isMinimized: !win.isMinimized })}
              >
                {win.isMinimized ? '□' : '−'}
              </button>
              <button
                className="w-5 h-5 flex items-center justify-center rounded hover:bg-destructive/20 text-destructive text-xs"
                onClick={() => onCloseWindow(win.id)}
              >
                ×
              </button>
            </div>
          </div>

          {/* 内容区 */}
          {!win.isMinimized && (
            <div className="overflow-auto" style={{ height: win.size.height - 36 }}>
              {renderContent(win)}
            </div>
          )}
        </div>
      ))}
    </>
  )
}
