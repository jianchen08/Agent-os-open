/**
 * 工作区标签右键上下文菜单
 *
 * 提供关闭 / 关闭其他标签 / 关闭所有标签 三项操作。
 * 与现有 `FileTreeContextMenu` 保持一致的自定义浮层风格：
 * position:fixed + 点击外部关闭 + Esc 关闭。
 *
 * 约定：pinned（固定）标签不可关闭，遵循 tab 上 `×` 按钮的可见性逻辑。
 *
 * @module WorkspaceTabContextMenu
 */

import { X, XCircle, Layers } from 'lucide-react'
import React, { useEffect, useRef, useState } from 'react'
import type { WorkspaceTab } from '@/types/layout'

/** 菜单项定义 */
interface MenuItem {
  /** 唯一标识 */
  id: 'close' | 'close-others' | 'close-all'
  /** 显示文本 */
  label: string
  /** 图标 */
  icon: React.ReactNode
  /** 是否显示 */
  visible: boolean
}

/** 工作区标签右键菜单属性 */
export interface WorkspaceTabContextMenuProps {
  /** 菜单定位 X（clientX） */
  x: number
  /** 菜单定位 Y（clientY） */
  y: number
  /** 右键触发的目标 tab */
  tab: WorkspaceTab
  /** 当前工作区 tab 列表（用于判定「关闭其他/所有」是否可用） */
  tabs: WorkspaceTab[]
  /** 关闭单个 tab（非 pinned） */
  onCloseTab: (tabId: string) => void
  /** 关闭其他可关 tab，保留 keepTabId 与所有 pinned */
  onCloseOthers: (keepTabId: string) => void
  /** 关闭所有可关（!pinned）tab */
  onCloseAll: () => void
  /** 关闭菜单回调 */
  onClose: () => void
}

/**
 * 工作区标签右键上下文菜单组件
 *
 * @returns 上下文菜单渲染结果
 */
export function WorkspaceTabContextMenu(props: WorkspaceTabContextMenuProps): React.ReactNode {
  const { x, y, tab, tabs, onCloseTab, onCloseOthers, onCloseAll, onClose } = props

  const menuRef = useRef<HTMLDivElement>(null)

  /** 浮层尺寸估算（用于视口边界夹取，避免菜单溢出屏幕） */
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: x, top: y })
  useEffect(() => {
    // 估算菜单宽高并做边界夹取
    const MENU_WIDTH = 176
    const MENU_HEIGHT = 96
    const margin = 8
    const vw = window.innerWidth
    const vh = window.innerHeight
    const left = Math.min(x, vw - MENU_WIDTH - margin)
    const top = Math.min(y, vh - MENU_HEIGHT - margin)
    setPos({ left: Math.max(margin, left), top: Math.max(margin, top) })
  }, [x, y])

  /** 点击外部关闭菜单（延迟绑定，避免触发菜单的右键事件立即关闭） */
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleClickOutside)
    }, 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [onClose])

  /** Esc 关闭 */
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  /** 可关闭的 tab（非 pinned）数量，用于判定菜单项可见性 */
  const closableTabs = tabs.filter((t) => !t.isPinned)
  const hasOtherClosable = closableTabs.some((t) => t.id !== tab.id)

  const menuItems: MenuItem[] = [
    {
      id: 'close',
      label: '关闭',
      icon: <X className="h-3.5 w-3.5" />,
      visible: !tab.isPinned,
    },
    {
      id: 'close-others',
      label: '关闭其他标签',
      icon: <Layers className="h-3.5 w-3.5" />,
      visible: hasOtherClosable,
    },
    {
      id: 'close-all',
      label: '关闭所有标签',
      icon: <XCircle className="h-3.5 w-3.5" />,
      visible: closableTabs.length > 0,
    },
  ]

  const visibleItems = menuItems.filter((item) => item.visible)

  /** 全部隐藏（如右键 pinned 且无其他可关 tab）时不渲染 */
  if (visibleItems.length === 0) return null

  const handleClick = (id: MenuItem['id']) => {
    switch (id) {
      case 'close':
        if (!tab.isPinned) onCloseTab(tab.id)
        break
      case 'close-others':
        onCloseOthers(tab.id)
        break
      case 'close-all':
        onCloseAll()
        break
    }
    onClose()
  }

  return (
    <div
      ref={menuRef}
      className="bg-background z-50 min-w-40 rounded-lg border py-1 shadow-xl"
      style={{
        position: 'fixed',
        left: pos.left,
        top: pos.top,
      }}
    >
      {visibleItems.map((item) => (
        <button
          key={item.id}
          className="text-foreground hover:bg-accent flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors"
          onClick={() => handleClick(item.id)}
        >
          {item.icon}
          <span>{item.label}</span>
        </button>
      ))}
    </div>
  )
}
