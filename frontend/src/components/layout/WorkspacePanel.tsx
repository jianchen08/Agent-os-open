/**
 * 工作区面板
 *
 * 管理工作区 Tab 切换，支持从悬浮窗拖拽吸附
 */

import { Maximize2, Minimize2 } from 'lucide-react'
import React, { useCallback } from 'react'
import type { WorkspaceTab } from '@/types/layout'

/** 工作区面板属性 */
interface WorkspacePanelProps {
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
}

/**
 * 工作区面板组件
 *
 * 显示 Tab 栏和对应的 Tab 内容区域
 */
export function WorkspacePanel({
  tabs,
  onTabChange,
  onTabClose,
  renderTabContent,
  onFullscreen,
  isFullscreen,
}: WorkspacePanelProps) {
  const handleTabWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault()
      el.scrollLeft += e.deltaY
    }
  }, [])

  if (tabs.length === 0) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        工作区为空 — 模块激活后自动出现
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* Tab 栏 */}
      <div className="border-border flex flex-shrink-0 items-center border-b">
        <div onWheel={handleTabWheel} className="flex min-w-0 flex-1 items-center overflow-x-auto">
        {tabs.map((tab) => (
          <div
            key={tab.id}
            className={`flex cursor-pointer items-center gap-1.5 border-b-2 px-3 py-2 text-sm whitespace-nowrap transition-colors ${
              tab.isActive
                ? 'border-primary text-foreground font-medium'
                : 'text-muted-foreground hover:text-foreground border-transparent'
            }`}
            onClick={() => onTabChange(tab.id)}
          >
            {tab.icon && <span>{tab.icon}</span>}
            <span>{tab.title}</span>
            {!tab.isPinned && (
              <button
                className="hover:bg-accent text-muted-foreground ml-1 flex h-4 w-4 items-center justify-center rounded text-xs"
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
        {/* 全屏按钮 */}
        {onFullscreen && (
          <button
            className="hover:bg-accent text-muted-foreground mx-1 flex h-7 w-7 shrink-0 items-center justify-center rounded transition-colors"
            onClick={onFullscreen}
            title={isFullscreen ? '退出全屏' : '铺满全屏'}
          >
            {isFullscreen ? (
              <Minimize2 className="h-3.5 w-3.5" />
            ) : (
              <Maximize2 className="h-3.5 w-3.5" />
            )}
          </button>
        )}
      </div>

      {/* Tab 内容 */}
      {/* BUG-FIX-fix_20260623_tab_reload:
          问题根因: 此前只渲染 activeTab 的内容，切换标签时旧标签被卸载、
                    新标签重新挂载，导致文件树每次切换都重新请求 API、
                    丢失已加载的数据和展开状态。
          修复方案: 渲染所有标签内容，非激活标签用 hidden（display:none）隐藏，
                    保持各标签组件实例挂载不变，切换时只切换可见性，
                    从而保留每个标签的内部状态（文件树数据、展开节点、滚动位置等）。
                  注意: renderTabContent 以 tab.id 为 key，保证每个标签对应
                    稳定的 React 节点，避免因列表重排导致意外重挂载。 */}
      <div className="min-h-0 flex-1 overflow-hidden">
        {tabs.length === 0 ? (
          <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
            选择一个标签页
          </div>
        ) : (
          tabs.map((tab) => (
            <div
              key={tab.id}
              className={tab.isActive ? 'h-full' : 'hidden'}
              aria-hidden={!tab.isActive}
            >
              {renderTabContent(tab)}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
