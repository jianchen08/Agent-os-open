/**
 * Agent Tab 导航组件
 *
 * 显示多个 Agent 的标签页，支持切换和关闭
 * 支持三层 Agent 架构：L1 (主 Agent), L2 (Sub Agent), L3 (执行 Agent)
 */

import { Plus } from 'lucide-react'
import { useCallback } from 'react'
import { AgentTabItem } from './AgentTabItem'
import type { AgentTab as AgentTabType } from '@/types/task'

/** Agent Tab 数据接口 */
export interface AgentTab {
  id: string
  name: string
  status: 'running' | 'waiting_input' | 'completed' | 'failed'
  isActive: boolean
  unreadCount?: number
  canClose: boolean
  agentLevel: 1 | 2 | 3 | undefined
  agentName?: string
  taskId?: string
  path?: string[]
}

/** 从 task.ts 导入的完整类型 */
export type AgentTabFull = AgentTabType

export interface AgentTabBarProps {
  tabs: AgentTab[]
  onTabChange: (tabId: string) => void
  onTabClose?: (tabId: string) => void
  onNewChat?: () => void
  activeTab?: string
}

/** TabBar 主组件 */
export const AgentTabBar: React.FC<AgentTabBarProps> = ({
  tabs,
  onTabChange,
  onTabClose,
  onNewChat,
}) => {
  const handleTabClose = useCallback(
    (tabId: string) => {
      onTabClose?.(tabId)
    },
    [onTabClose],
  )

  return (
    <div className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2">
      {/* Tab 列表 */}
      <div className="scrollbar-hide flex flex-1 items-center gap-1.5 overflow-x-auto">
        {tabs.map((tab) => (
          <AgentTabItem
            key={tab.id}
            tab={tab}
            onClick={() => onTabChange(tab.id)}
            onClose={tab.canClose ? () => handleTabClose(tab.id) : undefined}
          />
        ))}
      </div>

      {/* 新建对话按钮 */}
      {onNewChat && (
        <button
          onClick={onNewChat}
          className="hover:bg-accent text-muted-foreground hover:text-foreground flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg transition-colors"
          title="新建对话"
        >
          <Plus className="h-4 w-4" />
        </button>
      )}
    </div>
  )
}
