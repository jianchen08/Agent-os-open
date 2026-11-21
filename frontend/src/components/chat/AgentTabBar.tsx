/**
 * Agent Tab 导航组件
 *
 * 显示多个 Agent 的标签页，支持切换、关闭、固定、拖拽换位、右键菜单（关闭本
 * 标签/其他/全部，批量关签先经确认层）与激活签滚动可见——能力集与工作区标签栏
 * 对齐，共用实现直接来自 `components/layout`（TabContextMenu /
 * TabBulkCloseDialog）与 `hooks`（useTabContextMenu / useActiveTabScroll），
 * 关闭语义单源在 `utils/tabCloseSemantics`。
 * 支持三层 Agent 架构：L1 (主 Agent), L2 (Sub Agent), L3 (执行 Agent)
 */

import { useCallback, useRef } from 'react'
import { Plus } from '@/assets/icons'
import {
  BAND_BUTTON_ICON_CLASS,
  BAND_BUTTON_IDLE_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
} from '@/components/layout/bandButton'
import { TabBulkCloseDialog } from '@/components/layout/TabBulkCloseDialog'
import { TabContextMenu, buildTabMenuItems } from '@/components/layout/TabContextMenu'
import { useActiveTabScroll } from '@/hooks/useActiveTabScroll'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
import { useTabContextMenu } from '@/hooks/useTabContextMenu'
import { cn } from '@/lib/utils'
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
  /** 是否固定（钉住）：固定签不给单签关闭入口、不受批量关签影响 */
  isPinned?: boolean
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
  /** 关闭其他标签（固定签与主对话签保留）——提供即启用右键菜单 */
  onCloseOtherTabs?: (tabId: string) => void
  /** 关闭全部标签（固定签与主对话签保留） */
  onCloseAllTabs?: () => void
  /** 切换固定态 */
  onTogglePin?: (tabId: string) => void
}

/** TabBar 主组件 */
export const AgentTabBar: React.FC<AgentTabBarProps> = ({
  tabs,
  onTabChange,
  onTabClose,
  onNewChat,
  onReorder,
  onCloseOtherTabs,
  onCloseAllTabs,
  onTogglePin,
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

  // 激活签滚动可见（与工作区标签栏同一实现）
  const activeTabId = tabs.find((t) => t.isActive)?.id
  const activeTabRef = useActiveTabScroll<HTMLDivElement>(activeTabId)

  /** 右键菜单 + 批量关签确认层（与工作区标签栏同一状态机） */
  const tabMenuState = useTabContextMenu({
    tabs,
    onCloseTab: handleTabClose,
    onCloseOtherTabs: (tabId) => onCloseOtherTabs?.(tabId),
    onCloseAllTabs: () => onCloseAllTabs?.(),
  })

  /** 菜单仅在批量关签/固定能力可用时挂上（不提供对应回调就不给死入口） */
  const contextMenuEnabled = !!(onCloseOtherTabs || onCloseAllTabs || onTogglePin)
  const openMenu = tabMenuState.menu

  return (
    <div
      className={cn('flex min-w-0 items-center justify-center', BAND_GAP_CLASS)}
      data-testid="agent-tab-bar"
    >
      {/* Tab 列表（role=tablist 为 ARIA 语义；DSH 皮肤的 session.header.actions
          由适配器递送层转译到本组件锚点——位置映射，不贴 DSH 名字） */}
      <div
        ref={setScrollRef}
        className={cn(
          'scrollbar-hide flex items-center overflow-x-auto overflow-y-hidden',
          BAND_GAP_CLASS,
        )}
        role="tablist"
      >
        {tabs.map((tab) => (
          <AgentTabItem
            key={tab.id}
            ref={tab.isActive ? activeTabRef : undefined}
            tab={tab}
            onClick={() => onTabChange(tab.id)}
            onClose={tab.canClose ? () => handleTabClose(tab.id) : undefined}
            onReorder={onReorder}
            onContextMenu={contextMenuEnabled ? (e) => tabMenuState.openMenu(e, tab) : undefined}
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

      {/* 标签右键菜单（共用实现：条目语义与工作区标签栏同一份 buildTabMenuItems） */}
      {openMenu && (
        <TabContextMenu
          x={openMenu.x}
          y={openMenu.y}
          onClose={tabMenuState.closeMenu}
          items={buildTabMenuItems({
            testIdPrefix: 'agent-tab-menu',
            isPinned: openMenu.isPinned,
            canClose: openMenu.canClose,
            onTogglePin: onTogglePin
              ? () => {
                  onTogglePin(openMenu.tabId)
                  tabMenuState.closeMenu()
                }
              : undefined,
            onClose: () => tabMenuState.requestClose(openMenu.tabId),
            onCloseOther: onCloseOtherTabs
              ? () => tabMenuState.requestBulkClose('closeOther', openMenu.tabId)
              : undefined,
            onCloseAll: onCloseAllTabs
              ? () => tabMenuState.requestBulkClose('closeAll', openMenu.tabId)
              : undefined,
          })}
        />
      )}

      {/* 批量关签确认层（共用实现；固定签与主对话签保留如实标注） */}
      <TabBulkCloseDialog
        request={tabMenuState.bulkClose}
        keepNote="固定标签与主对话标签将保留"
        onConfirm={tabMenuState.confirmBulkClose}
        onCancel={tabMenuState.cancelBulkClose}
        testIdPrefix="agent-tab-bulk-close"
      />
    </div>
  )
}
