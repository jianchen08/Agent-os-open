/**
 * 工作区面板
 *
 * 管理工作区 Tab 切换，支持从悬浮窗拖拽吸附
 */

import React from 'react'
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
}

/**
 * 工作区面板组件
 *
 * 显示 Tab 栏和对应的 Tab 内容区域
 */
export function WorkspacePanel({ tabs, onTabChange, onTabClose, renderTabContent }: WorkspacePanelProps) {
  const activeTab = tabs.find(t => t.isActive)

  if (tabs.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground text-sm">
        工作区为空 — 模块激活后自动出现
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col">
      {/* Tab 栏 */}
      <div className="flex-shrink-0 flex items-center border-b border-border overflow-x-auto">
        {tabs.map(tab => (
          <div
            key={tab.id}
            className={`flex items-center gap-1.5 px-3 py-2 text-sm cursor-pointer border-b-2 transition-colors whitespace-nowrap ${
              tab.isActive
                ? 'border-primary text-foreground font-medium'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
            onClick={() => onTabChange(tab.id)}
          >
            {tab.icon && <span>{tab.icon}</span>}
            <span>{tab.title}</span>
            {!tab.isPinned && (
              <button
                className="ml-1 w-4 h-4 flex items-center justify-center rounded hover:bg-accent text-muted-foreground text-xs"
                onClick={e => { e.stopPropagation(); onTabClose(tab.id) }}
              >
                ×
              </button>
            )}
          </div>
        ))}
      </div>

      {/* Tab 内容 */}
      <div className="flex-1 overflow-auto">
        {activeTab ? renderTabContent(activeTab) : (
          <div className="h-full flex items-center justify-center text-muted-foreground text-sm">
            选择一个标签页
          </div>
        )}
      </div>
    </div>
  )
}
