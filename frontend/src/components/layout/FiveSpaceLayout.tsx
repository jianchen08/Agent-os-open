/**
 * Five Space Layout Component
 *
 * Implements the five-rendering-space layout:
 *   Chat Panel (left) | Workspace Panel (right)
 *   Floating Windows (overlay)
 *   Dock Bar (bottom)
 *   Fullscreen Overlay
 *
 * Responsive design:
 * - Mobile: chat full-width, workspace hidden (accessible via dock)
 * - Tablet: chat 60%, workspace 40%
 * - Desktop+: chat 45%, workspace 55%
 *
 * Integrates with the existing layout sub-components:
 * - DockBar, FloatingWindowManager, FullscreenOverlay, WorkspacePanel, SplitPane
 */

import React, { useCallback, useMemo, useState, useEffect } from 'react'
import { LayoutGrid, PanelLeftClose, PanelLeftOpen, Maximize2 } from 'lucide-react'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { safeLoadLayout, resolveLayout } from '@/services/layout/resolver'
import { useThemeStore } from '@/stores/themeStore'
import { useUIStore } from '@/stores/uiStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { cn } from '@/lib/utils'
import { FloatingWindowManager } from './FloatingWindowManager'
import { WorkspacePanel } from './WorkspacePanel'
import { DockBar } from './DockBar'
import { FullscreenOverlay } from './FullscreenOverlay'
import { ConnectionStatusIndicator } from './ConnectionStatusIndicator'
import { schemaRegistry } from '@/services/schema/registry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { ResolvedLayout, ViewportBreakpoint, FloatingWindowInstance } from '@/types/layout'

/** Props for the FiveSpaceLayout component */
export interface FiveSpaceLayoutProps {
  /** Chat panel content (the existing chat interface) */
  chatContent: React.ReactNode

  /** Optional top nav content */
  topNavContent?: React.ReactNode

  /** Optional sidebar content */
  sidebarContent?: React.ReactNode

  /** Callback when layout mode toggle is requested */
  onToggleMode?: () => void
}

/**
 * Get viewport breakpoint from width
 */
function getBreakpoint(
  width: number,
  breakpoints: { mobile: number; tablet: number; desktop: number; widescreen: number },
): ViewportBreakpoint {
  if (width < breakpoints.mobile) return 'mobile'
  if (width < breakpoints.tablet) return 'tablet'
  if (width < breakpoints.desktop) return 'desktop'
  return 'widescreen'
}

/**
 * Five Space Layout Component
 *
 * Arranges the UI into five rendering spaces:
 * 1. Chat Panel (left) - existing chat functionality
 * 2. Workspace Panel (right) - initially empty, will host schema-rendered content
 * 3. Floating Windows - draggable, resizable overlay windows
 * 4. Dock Bar - bottom bar with tool shortcuts and status indicators
 * 5. Fullscreen Overlay - for immersive interactions
 */
export function FiveSpaceLayout({
  chatContent,
  topNavContent,
  sidebarContent,
  onToggleMode,
}: FiveSpaceLayoutProps) {
  const themeConfig = useThemeStore((s) => s.currentTheme)
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const [workspaceCollapsed, setWorkspaceCollapsed] = useState(false)
  const [viewportWidth, setViewportWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth : 1280,
  )

  // Store state
  const floatingWindows = useLayoutModeStore((s) => s.floatingWindows)
  const workspaceTabs = useLayoutModeStore((s) => s.workspaceTabs)
  const dockItems = useLayoutModeStore((s) => s.dockItems)
  const fullscreenActive = useLayoutModeStore((s) => s.fullscreenActive)
  const fullscreenTitle = useLayoutModeStore((s) => s.fullscreenTitle)
  const fullscreenContent = useLayoutModeStore((s) => s.fullscreenContent)
  const activeExecutions = useLayoutModeStore((s) => s.activeExecutions)
  const pendingInteractions = useLayoutModeStore((s) => s.pendingInteractions)
  const connectionStatus = useLayoutModeStore((s) => s.connectionStatus)
  const updateFloatingWindow = useLayoutModeStore((s) => s.updateFloatingWindow)
  const closeFloatingWindow = useLayoutModeStore((s) => s.closeFloatingWindow)
  const setActiveTab = useLayoutModeStore((s) => s.setActiveTab)
  const closeWorkspaceTab = useLayoutModeStore((s) => s.closeWorkspaceTab)
  const exitFullscreen = useLayoutModeStore((s) => s.exitFullscreen)

  // Layout resolution
  const layoutConfig = useMemo(() => safeLoadLayout((themeConfig as any)?.layout), [themeConfig])
  const resolved = useMemo(
    () => resolveLayout(layoutConfig, viewportWidth),
    [layoutConfig, viewportWidth],
  )
  const breakpoint = useMemo(
    () => getBreakpoint(viewportWidth, layoutConfig.breakpoints),
    [viewportWidth, layoutConfig.breakpoints],
  )

  const isMobile = breakpoint === 'mobile'
  const isTablet = breakpoint === 'tablet'

  // Track viewport width
  useEffect(() => {
    const handleResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  const toggleWorkspace = useCallback(() => setWorkspaceCollapsed((prev) => !prev), [])

  /**
   * 处理任务树节点点击事件
   *
   * 当用户点击任务树中的节点时：
   * - 容器任务（有子节点）→ 打开工作区 Tab（容器任务工作空间）
   * - 叶子任务 → 打开子 Agent 对话标签 + 打开父容器的工作区 Tab
   *
   * BUG-FIX-fix_20260507_container_click:
   * 容器任务不再直接 return，而是打开其专属工作空间 Tab。
   *
   * @param node - 被点击的树节点数据
   */
  const handleTaskNodeClick = useCallback((node: Record<string, unknown>) => {
    const taskId = (node.id as string) ?? ''
    const title = (node.title as string) ?? '子任务'
    const pipelineRunId = (node.pipeline_run_id as string) ?? undefined
    if (!taskId) return

    const children = node.children as unknown[] | undefined
    const isContainer = Array.isArray(children) && children.length > 0

    // ---- 容器任务：打开工作区 Tab ----
    if (isContainer) {
      const existingTab = workspaceTabs.find((t) => t.moduleId === taskId)
      if (existingTab) {
        setActiveTab(existingTab.id)
      } else {
        const tabId = `workspace-${taskId}`
        const layoutStore = useLayoutModeStore.getState()
        layoutStore.addWorkspaceTab({
          id: tabId,
          title: `${title} 工作空间`,
          icon: '📁',
          moduleId: taskId,
          isActive: true,
          isPinned: false,
        })
      }
      return
    }

    // ---- 叶子任务：打开对话标签 + 父容器工作空间 ----
    const agentTabStore = useAgentTabStore.getState()

    // 确保主 Agent 标签存在（Tab 导航需要至少 2 个 tab 才显示）
    const hasMainTab = agentTabStore.tabs.some((t) => t.agentLevel === 1)
    if (!hasMainTab && activeSessionId) {
      agentTabStore.openSubAgentTab({
        agentId: activeSessionId,
        agentName: '主 Agent',
        parentRecordId: activeSessionId,
        agentLevel: 1,
        taskId: activeSessionId,
        status: 'running',
        setActive: false,
      })
    }

    // 打开子任务标签
    agentTabStore.openSubAgentTab({
      agentId: taskId,
      agentName: title,
      parentRecordId: taskId,
      agentLevel: 2,
      taskId,
      status: (node.status as AgentTab['status']) ?? 'running',
      setActive: true,
      pipelineId: pipelineRunId,
    })

    // 加载子管道消息
    agentTabStore.loadTabMessages(`sub-${taskId}`, pipelineRunId)
  }, [activeSessionId, workspaceTabs, setActiveTab])

  // Build dynamic dock items with execution status
  const enrichedDockItems = useMemo(() => {
    const items = [...dockItems]

    // Add execution status items if any are active
    for (const execution of activeExecutions) {
      if (execution.status === 'running') {
        items.push({
          id: `exec-${execution.id}`,
          moduleId: execution.id,
          icon: execution.type === 'tool' ? '🔧' : execution.type === 'agent' ? '🤖' : '⚡',
          label: execution.name,
          indicator: 'dot' as const,
          indicatorColor: 'var(--accent-waiting, #f59e0b)',
          isActive: true,
          onClick: () => {
            // Could open execution details in workspace panel
          },
        })
      }
    }

    // Add interaction request items
    for (const interaction of pendingInteractions) {
      items.push({
        id: `interaction-${interaction.id}`,
        moduleId: interaction.id,
        icon: '❓',
        label: 'Input Required',
        indicator: 'badge' as const,
        badgeCount: 1,
        isActive: true,
        onClick: () => {
          // Could focus the interaction panel
        },
      })
    }

    return items
  }, [dockItems, activeExecutions, pendingInteractions])

  /**
   * 渲染工作区 Tab 内容
   *
   * BUG-FIX-fix_20260505_001: 连接 Schema 渲染链路
   * 问题根因: renderTabContent 是纯占位符，不渲染真实内容
   * 修复方案: 通过 schemaRegistry 查找模块 Schema，通过 widgetRegistry 查找组件，渲染真实内容
   */
  const renderTabContent = useCallback(
    (tab: import('@/types/layout').WorkspaceTab) => {
      if (tab.moduleId) {
        const registration = schemaRegistry.get(tab.moduleId)
        if (registration) {
          const schema = registration.schema
          const spaceConfig = schema.rendering?.spaces?.find(
            (s: Record<string, unknown>) => s.space === 'workspace'
          )
          if (spaceConfig) {
            const widgetType = spaceConfig.widget as string
            const WidgetComponent = widgetRegistry.get(widgetType) ?? widgetRegistry.findFallback(widgetType)
            if (WidgetComponent) {
              return (
                <div className="h-full overflow-auto p-4">
                  <WidgetComponent
                    {...(spaceConfig.props as Record<string, unknown> ?? {})}
                    dataSource={spaceConfig.dataSource as string}
                    sessionId={activeSessionId}
                    refreshKey={connectionStatus?.lastConnectedAt ?? ''}
                    onNodeClick={(node: any) => handleTaskNodeClick(node)}
                  />
                </div>
              )
            }
          }
        }
      }
      return (
        <div className="flex h-full flex-col items-center justify-center p-4">
          <div className="text-muted-foreground mb-2 text-sm">{tab.title}</div>
          <div className="text-muted-foreground/60 text-xs">模块内容不可用</div>
        </div>
      )
    },
    [activeSessionId, handleTaskNodeClick],
  )

  // Render floating window content (placeholder)
  const renderFloatingContent = useCallback(
    (window: FloatingWindowInstance) => {
      return (
        <div className="flex h-full items-center justify-center p-4">
          <div className="text-muted-foreground text-sm">
            {window.title} - Content placeholder
          </div>
        </div>
      )
    },
    [],
  )

  // Handle ESC key for fullscreen exit
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && fullscreenActive) {
        exitFullscreen()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [fullscreenActive, exitFullscreen])

  return (
    <div
      className="bg-background text-foreground flex h-screen w-screen flex-col overflow-hidden"
      style={{ fontFamily: 'var(--font-family)' }}
    >
      {/* ---- Top Navigation Bar ---- */}
      <header
        className="border-border flex h-10 shrink-0 items-center border-b px-3"
      >
        {/* Left: sidebar toggle + title */}
        <div className="flex items-center gap-2">
          <button
            onClick={toggleSidebar}
            className="hover:bg-accent rounded p-1 transition-colors"
            title={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
          >
            {sidebarCollapsed ? (
              <PanelLeftOpen className="h-4 w-4" />
            ) : (
              <PanelLeftClose className="h-4 w-4" />
            )}
          </button>
          <h1 className="text-sm font-semibold">SuperTerminal</h1>
        </div>

        {/* Center: top nav content */}
        <div className="flex flex-1 items-center justify-center">
          {topNavContent}
        </div>

        {/* Right: connection status + layout toggle */}
        <div className="flex items-center gap-2">
          <ConnectionStatusIndicator compact={false} showLatency showQueue />

          {pendingInteractions.length > 0 && (
            <div className="flex items-center gap-1 rounded-md bg-status-running/10 px-2 py-0.5 text-xs text-status-running">
              <span className="font-bold">{pendingInteractions.length}</span>
              <span>pending</span>
            </div>
          )}

          <button
            onClick={onToggleMode}
            className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors"
            title="Switch to classic layout"
          >
            <LayoutGrid className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Classic</span>
          </button>
        </div>
      </header>

      {/* ---- Main Content Area ---- */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Sidebar */}
        {sidebarContent && (
          <aside
            className={cn(
              'border-border flex shrink-0 flex-col overflow-hidden border-r transition-all duration-300',
            )}
            style={{
              width: sidebarCollapsed ? '48px' : isMobile ? '0px' : '14rem', /* 14rem = w-56 */
              minWidth: sidebarCollapsed ? '48px' : isMobile ? '0px' : '14rem',
              maxWidth: sidebarCollapsed ? '48px' : isMobile ? '0px' : '14rem',
            }}
          >
            {sidebarContent}
          </aside>
        )}

        {/* Chat + Workspace panels */}
        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/* Chat Panel */}
          <section
            className={cn(
              'border-border flex shrink-0 flex-col overflow-hidden border-r transition-all duration-300',
              workspaceCollapsed || isMobile ? 'flex-1' : '',
            )}
            style={{
              width:
                workspaceCollapsed || isMobile
                  ? undefined
                  : `${resolved.chatPanel.width}px`,
              minWidth: isMobile ? 0 : resolved.chatPanel.minWidth,
            }}
          >
            {chatContent}
          </section>

          {/* Workspace toggle handle */}
          {!isMobile && (
            <button
              onClick={toggleWorkspace}
              className={cn(
                'border-border hover:bg-accent/50 flex w-4 shrink-0 cursor-pointer items-center justify-center border-r transition-colors',
                workspaceCollapsed && 'hidden',
              )}
              title={workspaceCollapsed ? 'Show workspace' : 'Hide workspace'}
            >
              <span className="text-muted-foreground text-[10px]">
                {workspaceCollapsed ? '>' : '<'}
              </span>
            </button>
          )}

          {/* Workspace Panel */}
          {!isMobile && !workspaceCollapsed && (
            <section className="min-w-0 flex-1 overflow-hidden">
              <WorkspacePanel
                tabs={workspaceTabs}
                onTabChange={setActiveTab}
                onTabClose={closeWorkspaceTab}
                renderTabContent={renderTabContent}
              />
            </section>
          )}
        </div>
      </div>

      {/* ---- Dock Bar ---- */}
      <div
        className="border-border flex shrink-0 items-center justify-center gap-1 border-t px-2"
        style={{ height: resolved.dockBar.height }}
      >
        <DockBar
          items={enrichedDockItems}
          iconSize={layoutConfig.dockBar.iconSize}
          iconGap={layoutConfig.dockBar.iconGap}
        />

        {/* Execution progress mini-bar */}
        {activeExecutions.length > 0 && (
          <div className="ml-auto flex items-center gap-2">
            {activeExecutions
              .filter((e) => e.status === 'running')
              .slice(0, 3)
              .map((execution) => (
                <div
                  key={execution.id}
                  className="flex items-center gap-1.5 rounded-md bg-muted/50 px-2 py-0.5"
                  title={`${execution.name}: ${execution.progress}%`}
                >
                  <div className="bg-muted h-1 w-12 overflow-hidden rounded-full">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{ width: `${execution.progress}%`, backgroundColor: 'var(--status-running-color, #0ea5e9)' }}
                    />
                  </div>
                  <span className="text-muted-foreground text-[10px]">
                    {execution.progress}%
                  </span>
                </div>
              ))}
          </div>
        )}
      </div>

      {/* ---- Floating Windows Container ---- */}
      <div
        className="pointer-events-none fixed inset-0"
        style={{ zIndex: layoutConfig.zIndex.floatingWindow }}
      >
        <FloatingWindowManager
          windows={floatingWindows}
          onUpdateWindow={updateFloatingWindow}
          onCloseWindow={closeFloatingWindow}
          renderContent={renderFloatingContent}
        />
      </div>

      {/* ---- Fullscreen Overlay ---- */}
      <FullscreenOverlay
        isActive={fullscreenActive}
        title={fullscreenTitle ?? undefined}
        onExit={exitFullscreen}
      >
        {fullscreenContent}
      </FullscreenOverlay>
    </div>
  )
}
