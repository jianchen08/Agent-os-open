/**
 * Agent Tab 导航组件
 *
 * 显示多个 Agent 的标签页，支持切换和关闭
 * 支持三层 Agent 架构：L1 (主 Agent), L2 (Sub Agent), L3 (执行 Agent)
 */

import { ChevronLeft, ChevronRight, Plus } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
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
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const [canScrollLeft, setCanScrollLeft] = useState(false)
  const [canScrollRight, setCanScrollRight] = useState(false)

  const updateScrollState = useCallback(() => {
    const el = scrollContainerRef.current
    if (!el) return
    setCanScrollLeft(el.scrollLeft > 0)
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
  }, [])

  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    updateScrollState()
    el.addEventListener('scroll', updateScrollState)
    const observer = new ResizeObserver(updateScrollState)
    observer.observe(el)
    return () => {
      el.removeEventListener('scroll', updateScrollState)
      observer.disconnect()
    }
  }, [updateScrollState, tabs])

  const scroll = useCallback((direction: 'left' | 'right') => {
    const el = scrollContainerRef.current
    if (!el) return
    el.scrollBy({ left: direction === 'left' ? -150 : 150, behavior: 'smooth' })
  }, [])

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
    <div className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2">
      {/* Tab 列表 */}
      <div
        ref={setScrollRef}
        className="scrollbar-hide flex flex-1 items-center gap-1.5 overflow-x-auto"
      >
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
