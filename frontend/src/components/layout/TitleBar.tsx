/**
 * TitleBar — Electron 主窗口窗口控制簇
 *
 * 原生标题栏已在 electron/main.ts 隐藏（Windows/macOS titleBarStyle hidden、
 * Linux frameless），本组件接管窗口控制：
 * - 仅 Electron 主窗口渲染（Web / 子浮窗由 isDesktopMainWindow 判定）；
 * - 形态 = 钉在视口右上角的固定控制簇（与布局顶带同排，不占独立栏位），
 *   挂载时给 <html> 加 has-custom-titlebar 类（index.css 据此定义
 *   --app-titlebar-height 供 toast 等让位），卸载时移除；
 * - 窗口拖拽由布局顶带承载（ChatContainer 顶部图标带行 app-drag-region）；
 * - 拖拽区双击最大化/还原、Win+方向键贴边由系统处理，最大化状态经
 *   windowControls.onMaximizedChange 同步按钮图标（覆盖系统路径触发）。
 */

import { Copy, Minus, Square, X } from 'lucide-react'
import { useLayoutEffect, useState } from 'react'
import type { ReactNode } from 'react'

/** Electron 主窗口（非子浮窗）判定：TitleBar 仅在此环境渲染 */
export function isDesktopMainWindow(): boolean {
  return (
    typeof window !== 'undefined' &&
    !!window.electronAPI &&
    window.electronAPI.isChildWindow !== true &&
    !!window.electronAPI.windowControls
  )
}

/** 窗口控制按钮（Windows 惯例：方形、贴合窗口边缘） */
const CONTROL_BUTTON_CLASS =
  'text-muted-foreground hover:bg-accent hover:text-foreground flex w-11 shrink-0 items-center justify-center transition-colors'
const CLOSE_BUTTON_CLASS =
  'text-muted-foreground hover:bg-destructive hover:text-destructive-foreground flex w-11 shrink-0 items-center justify-center transition-colors'

export function TitleBar(): ReactNode {
  const [isMaximized, setIsMaximized] = useState(false)
  // 环境门控：仅 Electron 主窗口渲染（Web / 子浮窗直接空，安全可挂任意处）
  const visible = isDesktopMainWindow()
  const controls = window.electronAPI?.windowControls

  // useLayoutEffect：首帧绘制前挂占位类，避免页面先按满视口布局再下移的闪动
  useLayoutEffect(() => {
    if (!controls) return
    document.documentElement.classList.add('has-custom-titlebar')
    let mounted = true
    controls
      .isMaximized()
      .then((maximized) => {
        if (mounted) setIsMaximized(maximized)
      })
      .catch((err: unknown) => {
        console.error('[TitleBar] 查询最大化状态失败:', err)
      })
    const unsubscribe = controls.onMaximizedChange(setIsMaximized)
    return () => {
      mounted = false
      unsubscribe()
      document.documentElement.classList.remove('has-custom-titlebar')
    }
  }, [controls])

  if (!visible || !controls) return null

  return (
    <div
      className="text-foreground fixed top-0 right-0 z-[2147483647] flex h-10 select-none items-stretch"
      data-testid="custom-titlebar"
    >
      <button
        type="button"
        aria-label="最小化"
        title="最小化"
        className={CONTROL_BUTTON_CLASS}
        onClick={() => void controls.minimize()}
      >
        <Minus className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        aria-label={isMaximized ? '还原' : '最大化'}
        title={isMaximized ? '还原' : '最大化'}
        className={CONTROL_BUTTON_CLASS}
        onClick={() => void controls.toggleMaximize()}
      >
        {isMaximized ? <Copy className="h-3 w-3" /> : <Square className="h-3 w-3" />}
      </button>
      <button
        type="button"
        aria-label="关闭"
        title="关闭"
        className={CLOSE_BUTTON_CLASS}
        onClick={() => void controls.close()}
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  )
}
