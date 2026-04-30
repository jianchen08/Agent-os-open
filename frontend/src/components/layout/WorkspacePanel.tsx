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
export function WorkspacePanel({
  tabs,
  onTabChange,
  onTabClose,
  renderTabContent,
}: WorkspacePanelProps) {
  const activeTab = tabs.find((t) => t.isActive)

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
      <div className="border-border flex flex-shrink-0 items-center overflow-x-auto border-b">
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

      {/* Tab 内容 */}
      <div className="flex-1 overflow-auto">
        {activeTab ? (
          renderTabContent(activeTab)
        ) : (
          <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
            选择一个标签页
          </div>
        )}
      </div>
    </div>
  )
}
