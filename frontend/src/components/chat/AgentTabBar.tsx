/**
 * Agent Tab 导航组件
 *
 * 显示多个 Agent 的标签页，支持切换和关闭
 * 支持三层 Agent 架构：L1 (主 Agent), L2 (Sub Agent), L3 (执行 Agent)
 */

import { useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'
import {
  BAND_BUTTON_ICON_CLASS,
  BAND_BUTTON_IDLE_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
} from '@/components/layout/bandButton'
import { Plus } from '@/assets/icons'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
import { AgentTabItem } from './AgentTabItem'
import type { AgentTabStatus } from '@/types/task'

/** Agent Tab 数据接口 */
export interface AgentTab {
  id: string
  name: string
  status: AgentTabStatus
  isActive: boolean
  unreadCount?: number
  canClose: boolean
  agentLevel: 1 | 2 | 3 | undefined
  agentName?: string
  taskId?: string
  path?: string[]
}

export interface AgentTabBarProps {
  tabs: AgentTab[]
  onTabChange: (tabId: string) => void
  onTabClose?: (tabId: string) => void
  onNewChat?: () => void
  activeTab?: string
  /** 拖拽换位回调（拖拽标签落到目标标签上时触发） */
  onReorder?: (dragTabId: string, targetTabId: string) => void
}

/** TabBar 主组件 */
export const AgentTabBar: React.FC<AgentTabBarProps> = ({
  tabs,
  onTabChange,
  onTabClose,
  onNewChat,
  onReorder,
}) => {
  const scrollContainerRef = useRef<HTMLDivElement>(null)

  const handleWheel = useCallback((e: WheelEvent) => {
    const el = scrollContainerRef.current
    if (!el) return
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault()
      el.scrollLeft += e.deltaY
    }
  }, [])

  // 以非被动方式绑定 wheel，使 preventDefault() 生效（React 默认的 onWheel 是被动的）
  const wheelRef = useNonPassiveWheel<HTMLDivElement>(handleWheel)
  // 复用 scrollContainerRef，把 ref 同时分给滚动状态逻辑与非被动 wheel 监听
  const setScrollRef = useCallback(
    (el: HTMLDivElement | null) => {
      scrollContainerRef.current = el
      wheelRef(el)
    },
    [wheelRef],
  )

  const handleTabClose = useCallback(
    (tabId: string) => {
      onTabClose?.(tabId)
    },
    [onTabClose],
  )

  return (
    <div className={cn('flex min-w-0 items-center justify-center', BAND_GAP_CLASS)} data-testid="agent-tab-bar">
      {/* Tab 列表（role=tablist 为 ARIA 语义；DSH 皮肤的 session.header.actions
          由适配器递送层转译到本组件锚点——位置映射，不贴 DSH 名字） */}
      <div
        ref={setScrollRef}
        className={cn('scrollbar-hide flex items-center overflow-x-auto overflow-y-hidden', BAND_GAP_CLASS)}
        role="tablist"
      >
        {tabs.map((tab) => (
          <AgentTabItem
            key={tab.id}
            tab={tab}
            onClick={() => onTabChange(tab.id)}
            onClose={tab.canClose ? () => handleTabClose(tab.id) : undefined}
            onReorder={onReorder}
          />
        ))}
      </div>

      {/* 新建对话按钮 */}
      {onNewChat && (
        <button
          onClick={onNewChat}
          className={`${BAND_ICON_BUTTON_CLASS} ${BAND_BUTTON_IDLE_CLASS}`}
          title="新建对话"
        >
          <Plus className={BAND_BUTTON_ICON_CLASS} />
        </button>
      )}
    </div>
  )
}
