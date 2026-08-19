/** 工作区面板 管理工作区 Tab 切换，支持从悬浮窗拖拽吸附 */

import React, { useEffect, useRef, useState } from 'react'
import { FullscreenIcon, FullscreenExitIcon, FolderTree } from '@/assets/icons'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import type { WorkspaceTab } from '@/types/layout'

/** 工作区面板属性 */
export interface WorkspacePanelProps {
  /** 工作区 Tab 列表 */
  tabs: WorkspaceTab[]
  /** Tab 切换回调 */
  onTabChange: (tabId: string) => void
  /** Tab 关闭回调 */
  onTabClose: (tabId: string) => void
  /** 渲染 Tab 内容的函数 */
  renderTabContent: (tab: WorkspaceTab) => React.ReactNode
  /** 全屏切换回调 */
  onFullscreen?: () => void
  /** 是否处于全屏状态 */
  isFullscreen?: boolean
 /** 已访问过（至少激活过一次）的 Tab ID 集合，用于懒挂载策略 PERF 只有当前激活 Tab 或曾访问过的 Tab */
  visitedTabIds?: string[]
}

/** 标签右键菜单状态 */
interface TabContextMenuState {
  x: number
  y: number
  tabId: string
  isPinned: boolean
}

/** 工作区面板组件 显示 Tab 栏和对应的 Tab 内容区域 */
export function WorkspacePanel({
  tabs,
  onTabChange,
  onTabClose,
  renderTabContent,
  onFullscreen,
  isFullscreen,
  visitedTabIds,
}: WorkspacePanelProps) {
  // 以非被动方式绑定 wheel，使 preventDefault() 生效（React 默认的 onWheel 是被动的）
  const tabScrollRef = useNonPassiveWheel<HTMLDivElement>((e) => {
    const el = e.currentTarget as HTMLDivElement | null
    if (!el) return
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault()
      el.scrollLeft += e.deltaY
    }
  })

  /** 标签右键菜单 */
  const [tabMenu, setTabMenu] = useState<TabContextMenuState | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  /** 打开标签右键菜单 */
  const handleTabContextMenu = (
    e: React.MouseEvent,
    tab: WorkspaceTab,
  ) => {
    e.preventDefault()
    e.stopPropagation()
    setTabMenu({ x: e.clientX, y: e.clientY, tabId: tab.id, isPinned: !!tab.isPinned })
  }

  /** 点击外部关闭菜单 */
  useEffect(() => {
    if (!tabMenu) return
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setTabMenu(null)
      }
    }
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setTabMenu(null)
    }
    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('keydown', handleKey)
    }
  }, [tabMenu])

  /** 菜单动作 */
  const handleMenuAction = (action: 'close' | 'closeOther' | 'closeAll') => {
    if (!tabMenu) return
    const store = useLayoutModeStore.getState()
    if (action === 'close') {
      store.closeWorkspaceTab(tabMenu.tabId)
    } else if (action === 'closeOther') {
      store.closeOtherWorkspaceTabs(tabMenu.tabId)
    } else {
      store.closeAllWorkspaceTabs()
    }
    setTabMenu(null)
  }

  if (tabs.length === 0) {
    return (
      <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-3 text-sm">
        <FolderTree className="text-muted-foreground/40 h-10 w-10" />
        <span>暂无内容 — 从下方打开任务管理</span>
        <button
          onClick={() => openWorkspacePanelByPath('/tasks')}
          className="bg-primary/15 text-primary hover:bg-primary/25 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
        >
          打开任务管理
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* Tab 栏 */}
      <div className="border-border flex flex-shrink-0 items-center border-b">
        <div ref={tabScrollRef} className="flex min-w-0 flex-1 items-center overflow-x-auto">
        {tabs.map((tab) => (
          <div
            key={tab.id}
            className={`flex cursor-pointer items-center gap-1.5 border-b-2 px-3 py-2 text-sm whitespace-nowrap transition-colors ${
              tab.isActive
                ? 'border-primary text-foreground font-medium'
                : 'text-muted-foreground hover:text-foreground border-transparent'
            }`}
            onClick={() => onTabChange(tab.id)}
            onContextMenu={(e) => handleTabContextMenu(e, tab)}
            data-testid={`workspace-tab-${tab.id}`}
          >
            <span>{tab.title}</span>
            {!tab.isPinned && (
              <button
                className="hover:bg-accent text-muted-foreground ml-1 flex h-4 w-4 items-center justify-center rounded text-xs"
                aria-label={`关闭 ${tab.title}`}
                title={`关闭 ${tab.title}`}
                data-testid={`workspace-tab-close-${tab.id}`}
                onClick={(e) => {
                  e.stopPropagation()
                  onTabClose(tab.id)
                }}
              >
                ×
              </button>
            )}
          </div>
        ))}
        </div>
        {/* 全屏按钮（全屏模式隐藏顶栏，故退出入口必须留在工作区内部） */}
        {onFullscreen && (
          <button
            className="hover:bg-accent text-muted-foreground mx-1 flex h-7 w-7 shrink-0 items-center justify-center rounded transition-colors"
            onClick={onFullscreen}
            title={isFullscreen ? '退出全屏' : '铺满全屏'}
            aria-label={isFullscreen ? '退出全屏' : '铺满全屏'}
            data-testid="workspace-toggle-fullscreen"
          >
            {isFullscreen ? (
              <FullscreenExitIcon className="h-3.5 w-3.5" />
            ) : (
              <FullscreenIcon className="h-3.5 w-3.5" />
            )}
          </button>
        )}
      </div>

      {/* 标签右键菜单 */}
      {tabMenu && (
        <div
          ref={menuRef}
          className="bg-popover text-popover-foreground shadow-lg fixed z-[100] min-w-[140px] rounded-lg border p-1 text-sm"
          style={{ left: tabMenu.x, top: tabMenu.y }}
        >
          <button
            className="hover:bg-accent text-muted-foreground hover:text-foreground flex w-full items-center rounded px-2.5 py-1.5 text-left text-xs disabled:opacity-40"
            disabled={tabMenu.isPinned}
            title={tabMenu.isPinned ? '固定标签不可关闭' : undefined}
            onClick={() => handleMenuAction('close')}
            data-testid="workspace-tab-menu-close"
          >
            关闭本标签
          </button>
          <button
            className="hover:bg-accent text-muted-foreground hover:text-foreground flex w-full items-center rounded px-2.5 py-1.5 text-left text-xs"
            onClick={() => handleMenuAction('closeOther')}
            data-testid="workspace-tab-menu-close-other"
          >
            关闭其他标签
          </button>
          <button
            className="hover:bg-accent text-muted-foreground hover:text-foreground flex w-full items-center rounded px-2.5 py-1.5 text-left text-xs"
            onClick={() => handleMenuAction('closeAll')}
            data-testid="workspace-tab-menu-close-all"
          >
            关闭所有标签
          </button>
        </div>
      )}

      {/* Tab 内容 — 懒挂载：仅激活 Tab 或已访问 Tab 渲染真实内容 */}
      <div className="min-h-0 flex-1 overflow-hidden">
        {tabs.length === 0 ? (
          <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
            选择一个标签页
          </div>
        ) : (
          tabs.map((tab) => {
            // 激活 Tab 或已访问过的 Tab 才渲染真实内容；其余 Tab 懒挂载，避免首屏卡死
            const shouldRender =
              tab.isActive || (visitedTabIds ?? []).includes(tab.id)
            if (!shouldRender) {
              return <div key={tab.id} aria-hidden="true" />
            }
            return (
              <div
                key={tab.id}
                className={tab.isActive ? 'h-full' : 'hidden'}
                aria-hidden={!tab.isActive}
              >
                {renderTabContent(tab)}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
