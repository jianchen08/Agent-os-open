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
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import {
  BAND_BUTTON_ICON_CLASS,
  BAND_BUTTON_IDLE_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
} from './bandButton'
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

/** 顶带窗口控制按钮：统一款式（同高度/同尺寸/同圆角/同悬停），
    个性化一律走主题令牌（rounded-md=var(--radius-md)、bg-accent 等主题变量），
    组件内不写按钮级差异样式 */
const CONTROL_BUTTON_CLASS = `${BAND_ICON_BUTTON_CLASS} ${BAND_BUTTON_IDLE_CLASS}`

export function TitleBar(): ReactNode {
  const [isMaximized, setIsMaximized] = useState(false)
  /** 顶带控制簇槽位：存在=portal 进顶带（拖拽容器子元素，自动 no-drag 洞）；
      不存在（登录页等无顶带路由）=保持视口右上 fixed 形态 */
  const [clusterSlot, setClusterSlot] = useState<HTMLElement | null>(null)
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
        // 记日志即止：最大化状态仅影响标题钮图标，下一次最大化/还原事件会纠偏
        // （OBS-R258-1 吞错误规则登记）
        console.error('[TitleBar] 查询最大化状态失败:', err)
      })
    const unsubscribe = controls.onMaximizedChange(setIsMaximized)
    // 顶带挂载晚于本组件（登录→主界面路由切换）：监听挂载事件后迁入槽位
    const syncSlot = () => setClusterSlot(document.getElementById('chat-top-band-cluster'))
    syncSlot()
    window.addEventListener('chat-top-band-mounted', syncSlot)
    return () => {
      mounted = false
      unsubscribe()
      window.removeEventListener('chat-top-band-mounted', syncSlot)
      document.documentElement.classList.remove('has-custom-titlebar')
    }
  }, [controls])

  if (!visible || !controls) return null

  const cluster = (
    <div className={cn('flex h-full select-none items-center', BAND_GAP_CLASS)}>
      <button
        type="button"
        aria-label="最小化"
        title="最小化"
        className={CONTROL_BUTTON_CLASS}
        onClick={() => void controls.minimize()}
      >
        <Minus className={BAND_BUTTON_ICON_CLASS} />
      </button>
      <button
        type="button"
        aria-label={isMaximized ? '还原' : '最大化'}
        title={isMaximized ? '还原' : '最大化'}
        className={CONTROL_BUTTON_CLASS}
        onClick={() => void controls.toggleMaximize()}
      >
        {isMaximized ? (
          <Copy className={BAND_BUTTON_ICON_CLASS} />
        ) : (
          <Square className={BAND_BUTTON_ICON_CLASS} />
        )}
      </button>
      <button
        type="button"
        aria-label="关闭"
        title="关闭"
        className={CONTROL_BUTTON_CLASS}
        onClick={() => void controls.close()}
      >
        <X className={BAND_BUTTON_ICON_CLASS} />
      </button>
    </div>
  )
  return clusterSlot ? (
    createPortal(
      <div data-testid="custom-titlebar">{cluster}</div>,
      clusterSlot,
    )
  ) : (
    <div
      className={cn(
        'text-foreground app-no-drag fixed top-0 right-0 z-[2147483647] flex h-10 select-none items-center',
        BAND_GAP_CLASS,
      )}
      data-testid="custom-titlebar"
    >
      {cluster}
    </div>
  )
}
