/** 工作区面板 管理工作区 Tab 切换，支持从悬浮窗拖拽吸附 */

import React, { useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Eye, FullscreenIcon, FullscreenExitIcon, PlusIcon } from '@/assets/icons'
import { isDetachable } from '@/components/schema/PageRenderer'
import { useActiveTabScroll } from '@/hooks/useActiveTabScroll'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
import { useSessionThemeScope } from '@/hooks/useSessionThemeScope'
import { useTabContextMenu } from '@/hooks/useTabContextMenu'
import { cn } from '@/lib/utils'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { windowManager } from '@/services/window/WindowManager'
import { WORKSPACE_NAV_TAB, openWorkspacePanel } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import {
  BAND_BUTTON_ACTIVE_CLASS,
  BAND_BUTTON_CLASS,
  BAND_BUTTON_ICON_CLASS,
  BAND_BUTTON_IDLE_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
  BAND_TAB_MAX_WIDTH_CLASS,
  BAND_TAB_MIN_WIDTH_CLASS,
  BAND_TAB_PANEL_WIDTH_CLASS,
  BAND_TAB_WIDTH_CLASS,
} from './bandButton'
import { TabBulkCloseDialog } from './TabBulkCloseDialog'
import { TAB_MENU_ITEM_CLASS, TabContextMenu, buildTabMenuItems } from './TabContextMenu'
import { TabLabel } from './TabLabel'
import { WorkspaceNavPage } from './WorkspaceNavPage'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
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
  /** 已访问过（至少激活过一次）的 Tab ID 集合，用于懒挂载策略：只有当前激活 Tab 或曾访问过的 Tab 才挂载 */
  visitedTabIds?: string[]
  /** 标签行末尾槽位（ReactNode）：宿主层塞入顶带款式的原生按钮（如移动覆盖层
   *  返回对话钮），与 + / 只看当前会话 同类同款同排——尺寸位置由 BAND 设计
   *  系统保证一致，宿主层不再需要绝对定位浮钮。仅非 portal 形态生效。 */
  bandTrailingSlot?: React.ReactNode
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
  bandTrailingSlot,
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

  /** 右键菜单 + 批量关签确认层（与对话流标签栏同一状态机：useTabContextMenu） */
  const tabMenuState = useTabContextMenu({
    // 批量关签影响面按全集（含被「只看当前会话」隐藏的签）计数——关签动作作用于
    // store 全集，确认层数量必须与真实影响面一致
    tabs,
    onCloseTab: (tabId) => useLayoutModeStore.getState().closeWorkspaceTab(tabId),
    onCloseOtherTabs: (tabId) => useLayoutModeStore.getState().closeOtherWorkspaceTabs(tabId),
    onCloseAllTabs: () => useLayoutModeStore.getState().closeAllWorkspaceTabs(),
  })

  /** 「只看当前会话」开关（默认开）：会话派生页签按归属会话过滤显隐；组件内
   *  状态不持久化——过滤只在视图层，隐藏≠关闭，store 的 workspaceTabs 不变 */
  const [sessionOnly, setSessionOnly] = useState(true)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  /** 可见页签集合：全局页签（无 ownerSessionId）任何会话下恒可见；会话派生
   *  页签仅归属当前会话时可见（activeSessionId 为 null 时会话派生签全部隐藏） */
  const visibleTabs = useMemo(
    () =>
      sessionOnly
        ? tabs.filter((t) => !t.ownerSessionId || t.ownerSessionId === activeSessionId)
        : tabs,
    [tabs, sessionOnly, activeSessionId],
  )
  const hiddenCount = tabs.length - visibleTabs.length

  // 激活页签被过滤隐藏时回退切换到第一个可见页签（沿用 setActiveTab 通道；
  // 隐藏的页签原样保留在 store，重开开关/切回会话即回来）
  useEffect(() => {
    if (!sessionOnly || visibleTabs.length === 0) return
    if (visibleTabs.some((t) => t.isActive)) return
    onTabChange(visibleTabs[0].id)
  }, [sessionOnly, visibleTabs, onTabChange])

  // 激活签滚动可见（useActiveTabScroll，与对话流标签栏同一实现）
  const activeTabId = tabs.find((t) => t.isActive)?.id
  const activeTabRef = useActiveTabScroll<HTMLDivElement>(activeTabId)

  /** 会话主题 override 作用域挂点（主题桥 §5.0：作用域仅聊天区+面板容器） */
  const panelThemeScopeRef = useSessionThemeScope<HTMLDivElement>()

  /** 打开标签右键菜单（菜单状态机与对话流标签栏同一实现） */
  const handleTabContextMenu = (e: React.MouseEvent, tab: WorkspaceTab) => {
    tabMenuState.openMenu(e, tab)
  }

  /** 菜单打开态（局部承接，渲染中不重复做可选链判定） */
  const openMenu = tabMenuState.menu

  /** 菜单目标签与其归属声明页（detachable 弹出入口的判定与目标） */
  const menuTargetTab = openMenu ? tabs.find((t) => t.id === openMenu.tabId) : undefined
  const menuPage: PageDeclaration | null = menuTargetTab?.pageId
    ? (contributionRegistry.getPage(menuTargetTab.pageId) ?? null)
    : null

  /** 弹出为浮窗（工作区页签专属条目：作为共用菜单的前置项，不改共用条目定义） */
  const handlePopout = () => {
    if (!menuPage) return
    windowManager.openPopout(menuPage)
    tabMenuState.closeMenu()
  }

  /** 拖拽中的标签（HTML5 DnD 的 dataTransfer 在 dragover 期不可读，用模块态中转） */
  const dragWsTab = { id: '' as string }

  /** 顶带标签行槽位：存在 → 标签行 portal 进顶带（与聊天标签同排，跨全屏切换
      节点保活）；槽位缺失（移动端/无顶带形态）或全屏（顶带工作区列被盖）→
      内联渲染（全屏退出按钮必须留在工作区内部） */
  const [bandTabsSlot, setBandTabsSlot] = useState<HTMLElement | null>(null)
  useLayoutEffect(() => {
    setBandTabsSlot(document.getElementById('chat-top-band-workspace-tabs'))
  }, [])

  /** 弹出入口显隐：页面声明了 detachable 且未显式禁止 popout（禁止时 openPopout 为 no-op，不给死入口） */
  const canPopout = (page: PageDeclaration | null): boolean =>
    !!page && isDetachable(page) && page.detachable?.popout !== false

  /** 「新建标签页」（+）：浏览器式——紧挨最后一个标签，新开/激活「导航」页签
   * （内容=WorkspaceNavPage 卡片网格，与空标签态同一内容源）；openWorkspacePanel
   * 按 id 幂等（已开则激活既有页签） */
  const handleOpenHub = () => openWorkspacePanel(WORKSPACE_NAV_TAB)

  // Tab 栏（role=tablist/tab/aria-selected 为 ARIA 语义；DSH 皮肤选择器
  // 由适配器递送层按位置转译到 [data-region="workspace"] [role="tablist"]——
  // portal 进顶带后由槽位自身的 data-region=workspace 保持选择器命中）
  // 顶带形态（portal 进顶带槽位）：无面板边框、占满带高、垂直居中；
  // 标签行可收缩（min-w-0 + shrink）——带宽不足时标签先压到下限、文字省略，
  // 全压到下限后由 tablist 自身横向滚动；行本身不收缩会把按钮顶出窗口。
  // 面板形态（无顶带槽位的回退/移动端）：保留底边框分隔，行不参与纵向收缩；
  // 标签 shrink-0 不压缩（保持内容自然宽），横向空间不足走签条滚动，文字不压字。
  const tabBar = (
    <div
      className={cn(
        'flex items-center',
        BAND_GAP_CLASS,
        bandTabsSlot ? 'h-full min-w-0 shrink' : 'border-border flex-shrink-0 border-b',
      )}
    >
      <div
        ref={tabScrollRef}
        className={cn(
          'scrollbar-hide flex min-w-0 flex-1 items-center overflow-x-auto overflow-y-hidden',
          BAND_GAP_CLASS,
        )}
        role="tablist"
      >
        {visibleTabs.map((tab) => (
          <div
            key={tab.id}
            ref={tab.isActive ? activeTabRef : undefined}
            role="tab"
            aria-selected={tab.isActive}
            draggable
            onDragStart={(e) => {
              dragWsTab.id = tab.id
              e.dataTransfer.effectAllowed = 'move'
              e.dataTransfer.setData('text/plain', tab.id)
            }}
            onDragOver={(e) => {
              if (dragWsTab.id && dragWsTab.id !== tab.id) e.preventDefault()
            }}
            onDrop={(e) => {
              e.preventDefault()
              const dragId = dragWsTab.id || e.dataTransfer.getData('text/plain')
              dragWsTab.id = ''
              if (dragId && dragId !== tab.id) {
                useLayoutModeStore.getState().reorderWorkspaceTabs(dragId, tab.id)
              }
            }}
            className={cn(
              BAND_BUTTON_CLASS,
              'cursor-pointer overflow-hidden whitespace-nowrap',
              // 200px 通用上限先放（tailwind-merge 后者胜：面板形态被下方
              // BAND_TAB_PANEL_WIDTH_CLASS 的 120px 覆写，顶带形态即终值）；
              // 压缩策略仅顶带形态：带宽与对话标签共享，压缩换同屏签数（shrink
              // 本是 flex 默认，类作契约声明）；面板形态（移动端整行独占，P1-2）
              // 可压缩到 64px 下限 + 120px 上限 + truncate 省略——长标签不再把
              // 右侧图标组挤出屏，省略号替代渐变遮蔽（压缩态 CJK 遮罩从字中间
              // 切开，R9 巡检 #7 同款压字面）。
              BAND_TAB_MAX_WIDTH_CLASS,
              bandTabsSlot
                ? cn(BAND_TAB_WIDTH_CLASS, BAND_TAB_MIN_WIDTH_CLASS)
                : BAND_TAB_PANEL_WIDTH_CLASS,
              tab.isActive ? BAND_BUTTON_ACTIVE_CLASS : BAND_BUTTON_IDLE_CLASS,
            )}
            title={tab.title}
            onClick={() => onTabChange(tab.id)}
            onContextMenu={(e) => handleTabContextMenu(e, tab)}
            data-testid={`workspace-tab-${tab.id}`}
          >
            <TabLabel title={tab.title} variant={bandTabsSlot ? 'fade' : 'truncate'} />
            {!tab.isPinned && (
              <button
                className="hover:bg-muted text-muted-foreground ml-1 flex h-4 w-4 shrink-0 items-center justify-center rounded text-xs"
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
        {/* 「新建标签页」：浏览器式紧挨最后一个标签；点击新开/激活「导航」页签
            （WorkspaceNavPage 卡片网格） */}
        <button
          type="button"
          className={`${BAND_ICON_BUTTON_CLASS} ${BAND_BUTTON_IDLE_CLASS}`}
          onClick={handleOpenHub}
          title="新建标签页"
          aria-label="新建标签页"
          data-testid="workspace-tab-new"
        >
          <PlusIcon className={BAND_BUTTON_ICON_CLASS} />
        </button>
      </div>
      {/* 「只看当前会话」开关（页签条右端）：会话派生页签按归属会话过滤，
            全局页签恒可见；title 提示当前被隐藏的标签数 */}
      <button
        type="button"
        className={`${BAND_ICON_BUTTON_CLASS} ${sessionOnly ? BAND_BUTTON_ACTIVE_CLASS : BAND_BUTTON_IDLE_CLASS}`}
        onClick={() => setSessionOnly((v) => !v)}
        title={
          sessionOnly
            ? hiddenCount > 0
              ? `已按当前会话隐藏 ${hiddenCount} 个标签（点击显示全部）`
              : '只看当前会话（当前没有被隐藏的标签）'
            : '显示全部标签（开启后按当前会话过滤）'
        }
        aria-label="只看当前会话"
        data-testid="workspace-session-filter-toggle"
      >
        <Eye className={BAND_BUTTON_ICON_CLASS} />
      </button>
      {/* 全屏按钮（全屏模式隐藏顶栏，故退出入口必须留在工作区内部） */}
      {onFullscreen && (
        <button
          className={`${BAND_ICON_BUTTON_CLASS} ${BAND_BUTTON_IDLE_CLASS}`}
          onClick={onFullscreen}
          title={isFullscreen ? '退出全屏' : '铺满全屏'}
          aria-label={isFullscreen ? '退出全屏' : '铺满全屏'}
          data-testid="workspace-toggle-fullscreen"
        >
          {isFullscreen ? (
            <FullscreenExitIcon className={BAND_BUTTON_ICON_CLASS} />
          ) : (
            <FullscreenIcon className={BAND_BUTTON_ICON_CLASS} />
          )}
        </button>
      )}
      {/* 宿主层末尾槽位（移动覆盖层返回对话钮等）：band 原生按钮，同款同排 */}
      {bandTrailingSlot}
    </div>
  )

  return (
    <div className="flex h-full flex-col">
      {bandTabsSlot ? createPortal(tabBar, bandTabsSlot) : tabBar}

      {/* 标签右键菜单（共用实现：条目语义与对话流标签栏同一份 buildTabMenuItems） */}
      {openMenu && (
        <TabContextMenu
          x={openMenu.x}
          y={openMenu.y}
          onClose={tabMenuState.closeMenu}
          leadingItems={
            /* 声明 detachable 的页面页签 → 弹出浮窗入口（工作区页签专属条目） */
            canPopout(menuPage) ? (
              <button
                type="button"
                role="menuitem"
                className={TAB_MENU_ITEM_CLASS}
                onClick={handlePopout}
                data-testid="workspace-tab-menu-popout"
              >
                弹出为浮窗
              </button>
            ) : undefined
          }
          items={buildTabMenuItems({
            testIdPrefix: 'workspace-tab-menu',
            isPinned: openMenu.isPinned,
            canClose: openMenu.canClose,
            // 固定态切换复用既有 updateWorkspaceTab 通道（isPinned 数据语义不变，
            // 只是多了一个用户可达的开关）
            onTogglePin: () =>
              useLayoutModeStore.getState().updateWorkspaceTab(openMenu.tabId, {
                isPinned: !openMenu.isPinned,
              }),
            onClose: () => tabMenuState.requestClose(openMenu.tabId),
            onCloseOther: () => tabMenuState.requestBulkClose('closeOther', openMenu.tabId),
            onCloseAll: () => tabMenuState.requestBulkClose('closeAll', openMenu.tabId),
          })}
        />
      )}

      {/* 批量关签确认层（共用实现；固定签保留如实标注） */}
      <TabBulkCloseDialog
        request={tabMenuState.bulkClose}
        onConfirm={tabMenuState.confirmBulkClose}
        onCancel={tabMenuState.cancelBulkClose}
      />

      {/* Tab 内容 — 懒挂载：仅激活 Tab 或已访问 Tab 渲染真实内容（与页签条同用
          可见集合：被「只看当前会话」过滤的页签不渲染，原样保留在 store） */}
      <div ref={panelThemeScopeRef} className="min-h-0 flex-1 overflow-hidden">
        {visibleTabs.length === 0 ? (
          // 无可见页签 → 导航页兜底（schema 声明的 workspace 页面分组导航）
          <WorkspaceNavPage />
        ) : (
          visibleTabs.map((tab) => {
            // 激活 Tab 或已访问过的 Tab 才渲染真实内容；其余 Tab 懒挂载，避免首屏卡死
            const shouldRender = tab.isActive || (visitedTabIds ?? []).includes(tab.id)
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
