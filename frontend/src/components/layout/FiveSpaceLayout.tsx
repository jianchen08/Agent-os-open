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

import { Minimize2, FolderOpen, PanelRightOpen } from 'lucide-react'
import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react'
import { getEditorForFile } from '@/config/fileEditors'
import { cn } from '@/lib/utils'
import apiClient from '@/services/api/client'
import { safeLoadLayout, resolveLayout } from '@/services/layout/resolver'
import { schemaRegistry } from '@/services/schema/registry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useChatInputStore } from '@/stores/chatInputStore'
import { getFileReviewData, removeFileReviewData, registerFileReview } from '@/stores/fileReviewRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThemeStore } from '@/stores/themeStore'
import { useUIStore } from '@/stores/uiStore'
import { AppHeader } from './AppHeader'
import { DockBar } from './DockBar'
import { FloatingWindowManager } from './FloatingWindowManager'
import { FullscreenOverlay } from './FullscreenOverlay'
import { WorkspacePanel } from './WorkspacePanel'
import { FileReviewTab } from '../review/FileReviewTab'
import type { ResolvedLayout, ViewportBreakpoint, FloatingWindowInstance, WorkspaceTab } from '@/types/layout'

/** Props for the FiveSpaceLayout component */
export interface FiveSpaceLayoutProps {
  /** Chat panel content (the existing chat interface) */
  chatContent: React.ReactNode

  /** Optional sidebar content */
  sidebarContent?: React.ReactNode

  /** Callback when layout mode toggle is requested */
  onToggleMode?: () => void

  /** Whether to show the theme panel */
  showThemePanel?: boolean

  /** Callback to toggle theme panel visibility */
  onShowThemePanel?: (show: boolean) => void

  /** 登出回调 */
  onLogout?: () => void
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
  sidebarContent,
  onToggleMode,
  showThemePanel = false,
  onShowThemePanel,
  onLogout,
}: FiveSpaceLayoutProps) {
  const themeConfig = useThemeStore((s) => s.currentTheme)
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const [workspaceCollapsed, setWorkspaceCollapsed] = useState(false)
  const [workspaceFullscreen, setWorkspaceFullscreen] = useState(false)
  /** 移动端工作区覆盖层是否打开 */
  const [mobileWorkspaceOpen, setMobileWorkspaceOpen] = useState(false)
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
  const workspaceDataVersion = useLayoutModeStore((s) => s.workspaceDataVersion)
  const updateFloatingWindow = useLayoutModeStore((s) => s.updateFloatingWindow)
  const closeFloatingWindow = useLayoutModeStore((s) => s.closeFloatingWindow)
  const setActiveTab = useLayoutModeStore((s) => s.setActiveTab)
  const closeWorkspaceTab = useLayoutModeStore((s) => s.closeWorkspaceTab)
  const exitFullscreen = useLayoutModeStore((s) => s.exitFullscreen)

  /** 工作区刷新 key，用于驱动 FileTreeWidget 等组件重新加载 */
  const workspaceRefreshKey = useMemo(
    () => `${connectionStatus?.lastConnectedAt ?? ''}-v${workspaceDataVersion}`,
    [connectionStatus?.lastConnectedAt, workspaceDataVersion],
  )

  /**
   * 处理工作区 Tab 关闭，对文件审批类型 Tab 进行额外的数据清理
   */
  const handleCloseTab = useCallback((tabId: string) => {
    const tab = useLayoutModeStore.getState().workspaceTabs.find(t => t.id === tabId)
    if (tab?.moduleId === '__file_review__') {
      removeFileReviewData(tabId)
    }
    closeWorkspaceTab(tabId)
  }, [closeWorkspaceTab])

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

  const mobileInitRef = useRef(false)

  useEffect(() => {
    const handleResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  useEffect(() => {
    if (!mobileInitRef.current && isMobile && !sidebarCollapsed) {
      mobileInitRef.current = true
      useUIStore.getState().setSidebarCollapsed(true)
    }
  }, [isMobile, sidebarCollapsed])

  const toggleWorkspace = useCallback(() => setWorkspaceCollapsed((prev) => !prev), [])
  const toggleWorkspaceFullscreen = useCallback(() => setWorkspaceFullscreen((prev) => !prev), [])

  /**
   * 处理任务树节点点击（对话按钮）。
   *
   * 点击节点的对话按钮时，打开子 Agent 标签页，加载
   * 子管道的对话历史。同时确保主 Agent 标签存在（用于 Tab 导航显示）。
   *
   * BUG-FIX-fix_20260507_container_click:
   * 问题根因: 容器任务（有子节点的父任务）点击后打开子标签，但它没有独立执行者，
   *          其执行者就是主管道，而主管道已经是默认活跃的。
   * 修复方案: 容器任务直接 return，不执行任何跳转操作。
   *
   * BUG-FIX-fix_20260509_task_open_and_level:
   * 问题根因: 1) 有独立管道的 L2 编排者任务（如"Li的编排者"）因有子节点被完全阻止打开
   *          2) agentLevel 硬编码为 2，导致所有标签都显示 L2，L3 执行者无法正确显示
   * 修复方案: 1) 仅阻止有子节点且无独立管道的纯容器任务
   *          2) 从后端 agent_level 字段解析正确的层级数字
   *
   * @param node - 被点击的树节点数据
   */
  const handleTaskNodeClick = useCallback((node: Record<string, unknown>) => {
    const taskId = (node.id as string) ?? ''
    const title = (node.title as string) ?? '子任务'
    const pipelineRunId = (node.pipeline_run_id as string) ?? undefined
    if (!taskId) return

    // BUG-FIX-fix_20260510_container_tab_duplicate:
    // 容器任务（task_scope=container）不能打开对话，否则会创建重复标签
    const taskScope = (node.task_scope as string) ?? 'non_container'
    if (taskScope === 'container') return

    // BUG-FIX-fix_20260509_task_open_and_level:
    // 仅阻止有子节点且无独立管道的纯容器任务；
    // 有 pipeline_run_id 的编排者任务（L2）即使有子节点也可打开
    const children = node.children as unknown[] | undefined
    const hasOwnPipeline = !!pipelineRunId
    if (Array.isArray(children) && children.length > 0 && !hasOwnPipeline) return

    // 从后端 agent_level 字段解析层级数字
    const agentLevelStr = (node.agent_level as string) ?? ''
    let agentLevel: 1 | 2 | 3 = 2
    if (agentLevelStr) {
      if (agentLevelStr === 'L1' || agentLevelStr === '1') agentLevel = 1
      else if (agentLevelStr === 'L3' || agentLevelStr === '3') agentLevel = 3
    }

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

    // 打开子任务标签（使用解析后的正确层级）
    agentTabStore.openSubAgentTab({
      agentId: taskId,
      agentName: title,
      parentRecordId: taskId,
      agentLevel,
      taskId,
      status: (node.status as AgentTab['status']) ?? 'running',
      setActive: true,
      pipelineId: pipelineRunId,
    })

    // BUG-FIX-fix_20260509_tab_blank:
    // 异步加载子管道消息；loadTabMessages 会将加载结果同步到 pipelineMessageStore，
    // 与 openSubAgentTab 中同步的缓存消息形成互补（缓存优先，API 刷新补充）
    agentTabStore.loadTabMessages(`sub-${taskId}`, pipelineRunId)
  }, [activeSessionId])

  // Build dynamic dock items with execution status
  const enrichedDockItems = useMemo(() => {
    const items = [...dockItems]

    if (isMobile) {
      for (const item of items) {
        if (item.moduleId) {
          const origOnClick = item.onClick
          item.onClick = () => {
            origOnClick?.()
            setWorkspaceFullscreen(true)
          }
        }
      }
    }

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
  }, [dockItems, pendingInteractions, isMobile])

  /**
   * 渲染工作区 Tab 内容
   *
   * BUG-FIX-fix_20260505_001: 连接 Schema 渲染链路
   * 问题根因: renderTabContent 是纯占位符，不渲染真实内容
   * 修复方案: 通过 schemaRegistry 查找模块 Schema，通过 widgetRegistry 查找组件，渲染真实内容
   */
  const renderTabContent = useCallback(
    (tab: WorkspaceTab) => {
      // 文件审批标签渲染
      if (tab.moduleId === '__file_review__') {
        const reviewData = getFileReviewData(tab.id)
        if (!reviewData) {
          return (
            <div className="flex h-full flex-col items-center justify-center p-4">
              <div className="text-muted-foreground text-sm">审批数据已过期</div>
            </div>
          )
        }
        const handleSendMessage = (message: string, quotedText?: string, quotedFile?: string) => {
          if (quotedText) {
            const insertText = `「${quotedFile ? `${quotedFile}: ` : ''}${quotedText}」`
            useChatInputStore.getState().requestInsert(insertText)
            return
          }
          if (message) {
            // BUG-FIX-fix_20260511_interaction_response_lost:
            // 问题根因: reviewData.pipelineId 可能为空，且不是 WebSocket 连接池的 key
            // 修复方案: 优先使用 sessionId 或当前活跃会话 ID
            const sid = reviewData.sessionId || useSessionStore.getState().activeSessionId || reviewData.pipelineId
            if (sid) {
              globalWS.sendInteractionResponse(sid, reviewData.requestId, {
                responseType: 'approved',
                feedback: message,
              })
            }
          }
        }
        const handleSubmitReview = (requestId: string, response: 'approved' | 'denied', feedback?: string) => {
          // BUG-FIX-fix_20260511_interaction_response_lost:
          // 问题根因: reviewData.pipelineId 可能为空，且不是 WebSocket 连接池的 key
          // 修复方案: 优先使用 sessionId 或当前活跃会话 ID
          const sid = reviewData.sessionId || useSessionStore.getState().activeSessionId || reviewData.pipelineId
          if (sid) {
            wsPool.sendInteractionResponse(sid, {
              requestId,
              responseType: response,
              feedback: feedback || '',
            })
          }
        }
        const handleOpenFolder = async () => {
          const containerId = reviewData.containerTaskId
          if (!containerId) return
          try {
            await apiClient.post(`/api/v1/workspaces/${containerId}/open`)
          } catch {
            // 静默失败
          }
        }
        return (
          <div className="relative h-full">
            {reviewData.containerTaskId && (
              <button
                className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md border bg-background/80 px-2 py-1 text-xs text-muted-foreground backdrop-blur-sm transition-colors hover:bg-accent hover:text-foreground"
                onClick={handleOpenFolder}
                title="在系统文件管理器中打开"
              >
                <FolderOpen className="h-3.5 w-3.5" />
                打开文件夹
              </button>
            )}
            <FileReviewTab
              fileContents={reviewData.fileContents}
              requestId={reviewData.requestId}
              mode={reviewData.mode}
              title={reviewData.title}
              pipelineId={reviewData.pipelineId}
              options={reviewData.options}
              onSendMessage={handleSendMessage}
              onSubmitReview={handleSubmitReview}
            />
          </div>
        )
      }
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
                <div className="h-full overflow-auto p-2 sm:p-4">
                  <WidgetComponent
                    {...(spaceConfig.props as Record<string, unknown> ?? {})}
                    dataSource={spaceConfig.dataSource as string}
                    sessionId={activeSessionId}
                    refreshKey={workspaceRefreshKey}
                    onNodeClick={(node: any) => handleTaskNodeClick(node)}
                  />
                </div>
              )
            }
          }
        }
      }
      // component-based 渲染路径：通过 tab.component 直接查找 widget
      if (tab.component) {
        const WidgetComponent = widgetRegistry.get(tab.component) ?? widgetRegistry.findFallback(tab.component)
        if (WidgetComponent) {
          /**
           * 处理工作空间文件树中的文件点击
           *
           * 加载文件内容并注册为文件审批 Tab，在工作区中以 FileReviewTab 组件展示。
           *
           * @param filePath - 文件相对路径（如 src/main.py）
           * @param fileName - 文件名（如 main.py）
           */
          const handleFileClick = async (filePath: string, fileName: string) => {
            const containerId = tab.dataSource?.replace('workspace://', '') || ''
            if (!containerId) return

            const editor = getEditorForFile(fileName)

            if (editor.id === 'text_editor') {
              const tabId = `review-file-${containerId}-${filePath.replace(/[/\\]/g, '_')}`
              const layoutStore = useLayoutModeStore.getState()

              try {
                const resp = await apiClient.get(`/api/v1/workspaces/${containerId}/file-content`, {
                  params: { path: filePath }
                })
                if (resp.data?.success) {
                  registerFileReview(tabId, {
                    requestId: `file-${containerId}-${filePath}`,
                    mode: 'conversation',
                    title: fileName,
                    pipelineId: '',
                    fileContents: { [filePath]: resp.data.content },
                    containerTaskId: containerId,
                  })
                  layoutStore.addWorkspaceTab({
                    id: tabId,
                    title: fileName,
                    icon: '📄',
                    moduleId: '__file_review__',
                    isActive: true,
                    isPinned: false,
                  })
                }
              } catch {
                // 静默失败
              }
            }
          }

          /** 处理在系统文件管理器中打开工作空间目录 */
          const handleOpenFolder = async () => {
            const containerId = tab.dataSource?.replace('workspace://', '') || ''
            if (!containerId) return
            try {
              await apiClient.post(`/api/v1/workspaces/${containerId}/open`)
            } catch {
              // 静默失败
            }
          }

          const folderContainerId = tab.dataSource?.replace('workspace://', '') || ''

          return (
            <div className="relative h-full">
              {folderContainerId && (
                <button
                  className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md border bg-background/80 px-2 py-1 text-xs text-muted-foreground backdrop-blur-sm transition-colors hover:bg-accent hover:text-foreground"
                  onClick={handleOpenFolder}
                  title="在系统文件管理器中打开"
                >
                  <FolderOpen className="h-3.5 w-3.5" />
                  打开文件夹
                </button>
              )}
              <div className="h-full overflow-auto p-2 sm:p-4">
                <WidgetComponent
                  dataSource={tab.dataSource}
                  sessionId={activeSessionId}
                  refreshKey={workspaceRefreshKey}
                  showStatus={false}
                  showProgress={false}
                  showSearch={true}
                  expandLevel={0}
                  nodeTitleField="name"
                  nodeChildrenField="children"
                  onFileClick={handleFileClick}
                />
              </div>
            </div>
          )
        }
      }
      return (
        <div className="flex h-full flex-col items-center justify-center p-4">
          <div className="text-muted-foreground mb-2 text-sm">{tab.title}</div>
          <div className="text-muted-foreground/60 text-xs">模块内容不可用</div>
        </div>
      )
    },
    [activeSessionId, handleTaskNodeClick, workspaceRefreshKey],
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

  // Handle ESC key for fullscreen exit and mobile workspace close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (fullscreenActive) {
          exitFullscreen()
        } else if (workspaceFullscreen) {
          setWorkspaceFullscreen(false)
        } else if (mobileWorkspaceOpen) {
          setMobileWorkspaceOpen(false)
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [fullscreenActive, exitFullscreen, workspaceFullscreen, mobileWorkspaceOpen])

  return (
    <div
      className="bg-background text-foreground flex w-screen flex-col overflow-hidden"
      style={{ fontFamily: 'var(--font-family)', height: '100dvh' }}
    >
      {workspaceFullscreen ? (
        <>
          <div className="border-border flex h-8 shrink-0 items-center justify-between border-b px-3">
            <span className="text-muted-foreground text-xs">
              {workspaceTabs.find((t) => t.isActive)?.title ?? '工作区'}
            </span>
            <button
              onClick={toggleWorkspaceFullscreen}
              className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-2 py-0.5 text-xs transition-colors"
              title="退出全屏 (Esc)"
            >
              <Minimize2 className="h-3.5 w-3.5" />
              <span>退出全屏</span>
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            <WorkspacePanel
              tabs={workspaceTabs}
              onTabChange={setActiveTab}
              onTabClose={handleCloseTab}
              renderTabContent={renderTabContent}
              onFullscreen={toggleWorkspaceFullscreen}
              isFullscreen={true}
            />
          </div>
        </>
      ) : (
        <>
          {/* ---- Top Navigation Bar (shared AppHeader) ---- */}
          <AppHeader
            onToggleMode={onToggleMode ?? (() => {})}
            modeLabel="Classic"
            showThemePanel={showThemePanel}
            onShowThemePanel={onShowThemePanel ?? (() => {})}
            onLogout={onLogout ?? (() => {})}
            extraRight={
              pendingInteractions.length > 0 ? (
                <div className="flex items-center gap-1 rounded-md bg-status-running/10 px-2 py-0.5 text-xs text-status-running">
                  <span className="font-bold">{pendingInteractions.length}</span>
                  <span>pending</span>
                </div>
              ) : undefined
            }
          />

          {/* ---- Main Content Area ---- */}
          <div className="relative flex min-h-0 flex-1 overflow-hidden">
            {/* 移动端侧边栏：覆盖抽屉模式（从导航栏下方开始，不遮盖导航栏） */}
            {sidebarContent && isMobile && !sidebarCollapsed && (
              <div className="fixed inset-0 z-40" style={{ top: '2.5rem' }}>
                {/* 背景遮罩，点击关闭侧边栏 */}
                <div
                  className="absolute inset-0 bg-black/50"
                  onClick={() => useUIStore.getState().setSidebarCollapsed(true)}
                />
                {/* 侧边栏面板 */}
                <aside className="absolute left-0 top-0 bottom-0 z-50 flex w-72 flex-col border-r bg-background shadow-xl">
                  {sidebarContent}
                </aside>
              </div>
            )}

            {/* 桌面端侧边栏：内嵌模式 */}
            {sidebarContent && (
              <aside
                className={cn(
                  'border-border hidden shrink-0 flex-col overflow-hidden border-r transition-all duration-300 md:flex',
                )}
                style={{
                  width: sidebarCollapsed ? '48px' : '14rem',
                  minWidth: sidebarCollapsed ? '48px' : '14rem',
                  maxWidth: sidebarCollapsed ? '48px' : '14rem',
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
                  'border-border flex flex-col overflow-hidden transition-all duration-300',
                  // 桌面端保持 shrink-0 和 border-r；移动端移除两者以允许 flex-1 完全占满
                  !isMobile ? 'shrink-0 border-r' : '',
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

              {/* Workspace toggle handle - always visible */}
              {!isMobile && (
                <button
                  onClick={toggleWorkspace}
                  className={cn(
                    'border-border hover:bg-accent relative flex shrink-0 cursor-pointer items-center justify-center border-r transition-colors',
                    'hover:shadow-[2px_0_8px_rgba(0,0,0,0.1)]',
                    'active:bg-accent/80',
                    workspaceCollapsed ? 'w-8' : 'w-4',
                  )}
                  style={{ minHeight: '100%' }}
                  title={workspaceCollapsed ? 'Show workspace' : 'Hide workspace'}
                >
                  <span className="text-muted-foreground select-none" style={{ fontSize: workspaceCollapsed ? 16 : 10 }}>
                    {workspaceCollapsed ? '›' : '‹'}
                  </span>
                </button>
              )}

              {/* Workspace Panel */}
              {!isMobile && !workspaceCollapsed && (
                <section className="min-w-0 flex-1 overflow-hidden">
                  <WorkspacePanel
                    tabs={workspaceTabs}
                    onTabChange={setActiveTab}
                    onTabClose={handleCloseTab}
                    renderTabContent={renderTabContent}
                    onFullscreen={toggleWorkspaceFullscreen}
                    isFullscreen={false}
                  />
                </section>
              )}
            </div>
          </div>

          {/* ---- Dock Bar ---- */}
          <div
            className="border-border flex shrink-0 items-center gap-1 border-t px-2"
            style={{ height: resolved.dockBar.height }}
          >
            <DockBar
              items={enrichedDockItems}
              iconSize={layoutConfig.dockBar.iconSize}
              iconGap={layoutConfig.dockBar.iconGap}
              showLabels={layoutConfig.dockBar.showLabels}
            />


          </div>

          {/* 移动端工作区全屏覆盖层 */}
          {isMobile && mobileWorkspaceOpen && (
            <div className="fixed inset-0 z-30 flex flex-col bg-background" style={{ top: '2.5rem' }}>
              {/* 工作区顶部操作栏 */}
              <div className="border-border flex h-9 shrink-0 items-center justify-between border-b px-2">
                <span className="text-foreground text-xs font-medium">工作区</span>
                <button
                  onClick={() => setMobileWorkspaceOpen(false)}
                  className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors"
                  title="关闭工作区"
                >
                  <Minimize2 className="h-3.5 w-3.5" />
                  <span>关闭</span>
                </button>
              </div>
              {/* 工作区内容 */}
              <div className="min-h-0 flex-1 overflow-hidden">
                <WorkspacePanel
                  tabs={workspaceTabs}
                  onTabChange={setActiveTab}
                  onTabClose={(tabId) => {
                    handleCloseTab(tabId)
                    const remaining = useLayoutModeStore.getState().workspaceTabs.filter(t => t.id !== tabId)
                    if (remaining.length === 0) {
                      setMobileWorkspaceOpen(false)
                    }
                  }}
                  renderTabContent={renderTabContent}
                  onFullscreen={toggleWorkspaceFullscreen}
                  isFullscreen={false}
                />
              </div>
            </div>
          )}
        </>
      )}

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
