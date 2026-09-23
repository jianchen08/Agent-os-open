/** 工作区面板 管理工作区 Tab 切换，支持从悬浮窗拖拽吸附 */

import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { FullscreenIcon, FullscreenExitIcon, PlusIcon } from '@/assets/icons'
import { isDetachable } from '@/components/schema/PageRenderer'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
import { cn } from '@/lib/utils'
import { TabLabel } from './TabLabel'
import {
  BAND_BUTTON_ACTIVE_CLASS,
  BAND_BUTTON_CLASS,
  BAND_BUTTON_ICON_CLASS,
  BAND_BUTTON_IDLE_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
  BAND_TAB_MAX_WIDTH_CLASS,
  BAND_TAB_MIN_WIDTH_CLASS,
  BAND_TAB_WIDTH_CLASS,
} from './bandButton'
import { useSessionThemeScope } from '@/hooks/useSessionThemeScope'
import { WORKSPACE_NAV_TAB, openWorkspacePanel } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import { windowManager } from '@/services/window/WindowManager'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { createPortal } from 'react-dom'
import type { WorkspaceTab } from '@/types/layout'
import { WorkspaceNavPage } from './WorkspaceNavPage'

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
}

/** 标签右键菜单状态 */
interface TabContextMenuState {
  x: number
  y: number
  tabId: string
  isPinned: boolean
  /** 页签归属的声明页（detachable 弹出入口的判定与目标；非声明页签为 null） */
  page: PageDeclaration | null
}

/** 批量关签确认层状态（BUG-79 实证入口防误触：批量关闭不可逆，先确认影响面） */
interface CloseConfirmState {
  action: 'closeOther' | 'closeAll'
  /** 关签基准签（closeOther 保留目标签） */
  tabId: string
  /** 将被关闭的页签数（固定签保留，如实排除） */
  count: number
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
  /** 批量关签确认层（closeOther/closeAll 专用；关闭本标签单签可逆不拦） */
  const [closeConfirm, setCloseConfirm] = useState<CloseConfirmState | null>(null)

  /** 会话主题 override 作用域挂点（主题桥 §5.0：作用域仅聊天区+面板容器） */
  const panelThemeScopeRef = useSessionThemeScope<HTMLDivElement>()

  /** 打开标签右键菜单 */
  const handleTabContextMenu = (
    e: React.MouseEvent,
    tab: WorkspaceTab,
  ) => {
    e.preventDefault()
    e.stopPropagation()
    const page = tab.pageId ? (contributionRegistry.getPage(tab.pageId) ?? null) : null
    setTabMenu({ x: e.clientX, y: e.clientY, tabId: tab.id, isPinned: !!tab.isPinned, page })
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
  const handleMenuAction = (action: 'close' | 'closeOther' | 'closeAll' | 'popout') => {
    if (!tabMenu) return
    const store = useLayoutModeStore.getState()
    if (action === 'close') {
      store.closeWorkspaceTab(tabMenu.tabId)
    } else if (action === 'closeOther' || action === 'closeAll') {
      // BUG-79 实证入口防误触：批量关签不可逆，先经确认层（文案标注影响数量）。
      // 影响数为 0（其余全固定/无签）时动作本身是 no-op，直接执行不加确认。
      const count =
        action === 'closeOther'
          ? tabs.filter((t) => t.id !== tabMenu.tabId && !t.isPinned).length
          : tabs.filter((t) => !t.isPinned).length
      setTabMenu(null)
      if (count === 0) {
        if (action === 'closeOther') store.closeOtherWorkspaceTabs(tabMenu.tabId)
        else store.closeAllWorkspaceTabs()
      } else {
        setCloseConfirm({ action, tabId: tabMenu.tabId, count })
      }
      return
    } else if (tabMenu.page) {
      // 声明 detachable 的页面 → 弹出浮窗（FloatingWindowManager 承载内容）
      windowManager.openPopout(tabMenu.page)
    }
    setTabMenu(null)
  }

  /** 确认层确认：执行挂起的批量关签 */
  const handleConfirmClose = () => {
    if (!closeConfirm) return
    const store = useLayoutModeStore.getState()
    if (closeConfirm.action === 'closeOther') store.closeOtherWorkspaceTabs(closeConfirm.tabId)
    else store.closeAllWorkspaceTabs()
    setCloseConfirm(null)
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
  // 面板形态（无顶带槽位的回退/移动端）：保留底边框分隔，行不参与纵向收缩。
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
        {tabs.map((tab) => (
          <div
            key={tab.id}
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
              BAND_TAB_WIDTH_CLASS,
              BAND_TAB_MIN_WIDTH_CLASS,
              BAND_TAB_MAX_WIDTH_CLASS,
              tab.isActive ? BAND_BUTTON_ACTIVE_CLASS : BAND_BUTTON_IDLE_CLASS,
            )}
            title={tab.title}
            onClick={() => onTabChange(tab.id)}
            onContextMenu={(e) => handleTabContextMenu(e, tab)}
            data-testid={`workspace-tab-${tab.id}`}
          >
            <TabLabel title={tab.title} />
            {!tab.isPinned && (
              <button
                className="hover:bg-accent text-muted-foreground ml-1 flex h-4 w-4 shrink-0 items-center justify-center rounded text-xs"
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
      </div>
  )

  return (
    <div className="flex h-full flex-col">
      {bandTabsSlot ? createPortal(tabBar, bandTabsSlot) : tabBar}

      {/* 标签右键菜单 */}
      {tabMenu && (
        <div
          ref={menuRef}
          className="bg-popover text-popover-foreground shadow-lg fixed z-[100] min-w-[140px] rounded-lg border p-1 text-sm"
          style={{ left: tabMenu.x, top: tabMenu.y }}
        >
          {/* 声明 detachable 的页面页签 → 弹出浮窗入口（FloatingWindowManager 基建复用） */}
          {canPopout(tabMenu.page) && (
            <button
              className="hover:bg-accent text-muted-foreground hover:text-foreground flex w-full items-center rounded px-2.5 py-1.5 text-left text-xs"
              onClick={() => handleMenuAction('popout')}
              data-testid="workspace-tab-menu-popout"
            >
              弹出为浮窗
            </button>
          )}
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

      {/* 批量关签确认层（BUG-79 实证入口防误触）：文案标注影响数量，确认才落关签 */}
      <Dialog open={!!closeConfirm} onOpenChange={(open) => !open && setCloseConfirm(null)}>
        <DialogContent className="max-w-[380px]">
          <DialogHeader>
            <DialogTitle>{closeConfirm?.action === 'closeOther' ? '关闭其他标签' : '关闭所有标签'}</DialogTitle>
            <DialogDescription>
              {closeConfirm?.action === 'closeOther'
                ? `将关闭其余 ${closeConfirm.count} 个标签，固定标签将保留。此操作不可批量恢复，确定继续吗？`
                : `将关闭全部 ${closeConfirm?.count ?? 0} 个标签，固定标签将保留。此操作不可批量恢复，确定继续吗？`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setCloseConfirm(null)}>
              取消
            </Button>
            <Button variant="destructive" size="sm" onClick={handleConfirmClose}>
              确认关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Tab 内容 — 懒挂载：仅激活 Tab 或已访问 Tab 渲染真实内容 */}
      <div ref={panelThemeScopeRef} className="min-h-0 flex-1 overflow-hidden">
        {tabs.length === 0 ? (
          // 无已开页签 → 导航页兜底（schema 声明的 workspace 页面分组导航）
          <WorkspaceNavPage />
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
