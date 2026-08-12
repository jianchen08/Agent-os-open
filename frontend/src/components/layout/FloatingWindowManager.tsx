/**
 * 悬浮窗管理器
 *
 * 管理多个悬浮窗实例，支持拖拽、调整大小和 z-index 层级管理
 */

import React, { useState, useCallback, useRef } from 'react'
import type { ReactNode } from 'react'
import type { FloatingWindowInstance } from '@/types/layout'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { renderPageContent } from '@/components/schema/PageRenderer'

/**
 * 从 FloatingWindowInstance 解析 pageId
 *
 * 优先取 win.props.pageId（WindowManager.openPopout 写入）；
 * 否则从 win.id 按 `${pageId}-popout-...` 约定拆分。
 */
function resolvePageId(win: FloatingWindowInstance): string | undefined {
  const fromProps = win.props?.pageId
  if (typeof fromProps === 'string' && fromProps) return fromProps
  if (typeof win.id === 'string' && win.id.includes('-popout-')) {
    return win.id.split('-popout-')[0]
  }
  return undefined
}

/**
 * 渲染悬浮窗内容（阶段5：接通 PageRenderer）
 *
 * 分发优先级：
 * 1. win → pageId → contributionRegistry.getPage → renderPageContent
 *    （复用 widget/schema 分发，page 内容完整复用）
 * 2. win.component → widgetRegistry.get（兼容旧 FloatingWindowInstance，
 *    未走 page 体系直接注册 widget 的场景）
 * 3. 兜底占位（不崩溃）
 *
 * FiveSpaceLayout 把此函数作为 renderContent 传给 FloatingWindowManager。
 */
export function renderFloatingWindowContent(win: FloatingWindowInstance): ReactNode {
  // 1) page 分发：复用 PageRenderer 的 widget/schema/dock 分发链路
  const pageId = resolvePageId(win)
  if (pageId) {
    const page = contributionRegistry.getPage(pageId)
    if (page) {
      return renderPageContent(page)
    }
  }

  // 2) 兼容旧实例：win.component 直接查 widgetRegistry
  if (win.component) {
    const Widget = widgetRegistry.get(win.component)
    if (Widget) {
      return <Widget {...(win.props ?? {})} />
    }
  }

  // 3) 兜底占位（不崩溃）
  return (
    <div className="text-muted-foreground flex h-full items-center justify-center p-4 text-sm">
      {win.title || win.id} - 内容不可用
    </div>
  )
}

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
export function FloatingWindowManager({
  windows,
  onUpdateWindow,
  onCloseWindow,
  renderContent,
}: FloatingWindowManagerProps) {
  const [dragState, setDragState] = useState<{
    windowId: string
    startX: number
    startY: number
    startPosX: number
    startPosY: number
  } | null>(null)

  /**
   * 处理悬浮窗拖拽开始
   *
   * 记录起始位置，注册全局 mousemove/mouseup 事件
   */
  const handleDragStart = useCallback(
    (windowId: string, e: React.MouseEvent) => {
      const win = windows.find((w) => w.id === windowId)
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
    },
    [windows, onUpdateWindow],
  )

  if (windows.length === 0) return null

  return (
    <>
      {windows.map((win) => (
        <div
          key={win.id}
          className="bg-background text-foreground border-border pointer-events-auto fixed overflow-hidden rounded-lg border shadow-lg"
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
            className="bg-muted/50 border-border flex cursor-move items-center justify-between border-b px-3 py-2"
            onMouseDown={(e) => handleDragStart(win.id, e)}
          >
            <span className="text-foreground truncate text-sm font-medium">
              {win.icon && <span className="mr-2">{win.icon}</span>}
              {win.title}
            </span>
            <div className="flex items-center gap-1">
              <button
                className="hover:bg-accent text-muted-foreground flex h-5 w-5 items-center justify-center rounded text-xs"
                onClick={() => onUpdateWindow(win.id, { isMinimized: !win.isMinimized })}
              >
                {win.isMinimized ? '□' : '−'}
              </button>
              <button
                className="hover:bg-destructive/20 text-destructive flex h-5 w-5 items-center justify-center rounded text-xs"
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
