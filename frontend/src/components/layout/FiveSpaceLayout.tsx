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

import { Minimize2, FolderOpen } from 'lucide-react'
import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react'
import { getEditorForFile } from '@/config/fileEditors'
import { cn } from '@/lib/utils'
import { Splitter } from 'antd'
import apiClient from '@/services/api/client'
import { safeLoadLayout, resolveLayout } from '@/services/layout/resolver'
import { schemaRegistry } from '@/services/schema/registry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { getFileEditorData, registerFileEditor, removeFileEditorData, updateFileEditorData, emitFileChange } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThemeStore } from '@/stores/themeStore'

import { useUIStore } from '@/stores/uiStore'
import { AppHeader } from './AppHeader'
import { DockBar } from './DockBar'
import { FloatingWindowManager } from './FloatingWindowManager'
import { FullscreenOverlay } from './FullscreenOverlay'
import { WorkspacePanel } from './WorkspacePanel'
import { CodeEditor } from '../workspace/CodeEditor'
import { FilePreview } from '../workspace/FilePreview'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import type { ResolvedLayout, ViewportBreakpoint, FloatingWindowInstance, WorkspaceTab } from '@/types/layout'
import type { AgentTab } from '@/types/task'

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
  const workspaceCollapsed = useUIStore((s) => s.workspaceCollapsed)
  const toggleWorkspace = useUIStore((s) => s.toggleWorkspace)
  const setWorkspaceCollapsed = useUIStore((s) => s.setWorkspaceCollapsed)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
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

  /** 使用 ref 保持最新的 workspaceRefreshKey，避免 renderTabContent 依赖变化导致 CodeEditor 重新挂载 */
  const workspaceRefreshKeyRef = useRef(workspaceRefreshKey)
  useEffect(() => {
    workspaceRefreshKeyRef.current = workspaceRefreshKey
  }, [workspaceRefreshKey])

  /**
   * 文件编辑器自动刷新逻辑
   *
   * 每 3 秒轮询检查已打开的文件编辑器 Tab 对应的文件是否被外部修改，
   * 若内容变化则通过事件机制通知 CodeEditor 组件更新。
   */
  useEffect(() => {
    const intervalMs = 3000
    const timer = setInterval(async () => {
      const tabs = useLayoutModeStore.getState().workspaceTabs
      const fileEditorTabs = tabs.filter(
        (t) => t.moduleId === '__file_editor__' && t.isActive
      )

      for (const tab of fileEditorTabs) {
        const editorData = getFileEditorData(tab.id)
        if (!editorData || !editorData.containerTaskId) continue

        try {
          const resp = await apiClient.get(
            `/api/v1/workspaces/${editorData.containerTaskId}/file-content`,
            { params: { path: editorData.filePath } }
          )
          if (resp.data?.success && resp.data.content !== undefined) {
            const newContent = resp.data.content
            const newSize = resp.data.size
            // 仅当内容真正变化时才更新
            if (newContent !== editorData.content) {
              updateFileEditorData(tab.id, {
                content: newContent,
                size: newSize,
              })
              emitFileChange(tab.id, newContent, newSize)
            }
          }
        } catch {
          // 静默失败，不影响用户体验
        }
      }
    }, intervalMs)

    return () => clearInterval(timer)
  }, [])

  /**
   * 处理工作区 Tab 关闭，清理 fileEditorRegistry 中对应的文件内容缓存
   */
  const handleCloseTab = useCallback((tabId: string) => {
    const tab = useLayoutModeStore.getState().workspaceTabs.find(t => t.id === tabId)
    if (tab?.moduleId === '__file_editor__') {
      removeFileEditorData(tabId)
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

  // BUG-FIX-fix_20260523_max_update_depth:
  // 问题根因: effect 内调用 setSidebarCollapsed(true) 修改了 sidebarCollapsed，
  //          而 sidebarCollapsed 又在依赖数组中，形成 effect → 修改依赖 → 重触发 → 无限循环。
  // 修复方案: 从依赖数组移除 sidebarCollapsed，改用 useUIStore.getState() 读取当前值，
  //          打破循环。
  // 影响范围: FiveSpaceLayout 移动端初始化逻辑
  // 修复日期: 2026-05-23
  useEffect(() => {
    if (!mobileInitRef.current && isMobile) {
      const currentCollapsed = useUIStore.getState().sidebarCollapsed
      if (!currentCollapsed) {
        mobileInitRef.current = true
        useUIStore.getState().setSidebarCollapsed(true)
      }
    }
  }, [isMobile])

  const toggleWorkspaceFullscreen = useCallback(() => setWorkspaceFullscreen((prev) => !prev), [])

  /**
   * 处理任务树节点点击（对话按钮）。
   *
   * 通过全局管道导航服务（pipelineNavigator）实现跨会话跳转：
   * 1. 获取节点的 pipelineRunId（核心标识）
   * 2. navigateToPipeline 在所有会话中查找管道归属并跳转
   * 3. 如果在其他会话 → 自动保存当前 Tab → 切换会话 → 创建/激活标签
   *
   * @param node - 被点击的树节点数据
   */
  const handleTaskNodeClick = useCallback(async (node: Record<string, unknown>) => {
    const taskId = (node.id as string) ?? ''
    const title = (node.title as string) ?? '子任务'
    const pipelineRunId = (node.pipeline_run_id as string) ?? undefined
    if (!taskId || !pipelineRunId) return

    const taskScope = (node.task_scope as string) ?? 'non_container'
    if (taskScope === 'container') return

    const agentLevelStr = (node.agent_level as string) ?? ''
    let agentLevel: 1 | 2 | 3 = 2
    if (agentLevelStr) {
      if (agentLevelStr === 'L1' || agentLevelStr === '1') agentLevel = 1
      else if (agentLevelStr === 'L3' || agentLevelStr === '3') agentLevel = 3
    }

    await navigateToPipeline(pipelineRunId, {
      agentName: title,
      agentLevel,
      taskId,
      status: (node.status as AgentTab['status']) ?? 'running',
    })

    if (isMobile) {
      setMobileWorkspaceOpen(false)
      setWorkspaceFullscreen(false)
      setWorkspaceCollapsed(true)
    }
  }, [activeSessionId, isMobile])

  // Build dock items from module schema (workspace tabs + external tool connections)
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

    return items
  }, [dockItems, isMobile])

  /**
   * 渲染工作区 Tab 内容
   *
   * BUG-FIX-fix_20260505_001: 连接 Schema 渲染链路
   * 问题根因: renderTabContent 是纯占位符，不渲染真实内容
   * 修复方案: 通过 schemaRegistry 查找模块 Schema，通过 widgetRegistry 查找组件，渲染真实内容
   */
  const renderTabContent = useCallback(
    (tab: WorkspaceTab) => {
      // 文件编辑器/预览器标签渲染
      if (tab.moduleId === '__file_editor__') {
        const editorData = getFileEditorData(tab.id)
        if (!editorData) {
          return (
            <div className="flex h-full flex-col items-center justify-center p-4">
              <div className="text-muted-foreground text-sm">文件数据已过期</div>
            </div>
          )
        }

        /** 保存文件内容到后端 */
        const handleSaveFile = async (content: string): Promise<boolean> => {
          const containerId = editorData.containerTaskId
          if (!containerId) return false
          try {
            const resp = await apiClient.put(
              `/api/v1/workspaces/${containerId}/file-content`,
              { content },
              { params: { path: editorData.filePath } },
            )
            const success = resp.data?.success ?? false
            if (success) {
              // 保存成功后更新注册表中的基准内容，避免后续轮询误判为外部修改
              updateFileEditorData(tab.id, { content })
            }
            return success
          } catch {
            return false
          }
        }

        const editor = getEditorForFile(editorData.filePath)

        if (editor.id === 'image_viewer') {
          return (
            <FilePreview
              filePath={editorData.filePath}
              content={editorData.content}
              size={editorData.size}
              containerTaskId={editorData.containerTaskId}
            />
          )
        }

        if (editor.id === 'html_preview') {
          return (
            <HtmlPreviewWidget
              html={editorData.content}
              filePath={editorData.filePath}
              title={editorData.fileName}
              containerTaskId={editorData.containerTaskId}
            />
          )
        }

        // PDF 预览
        const ext = editorData.filePath.substring(editorData.filePath.lastIndexOf('.')).toLowerCase()
        if (ext === '.pdf') {
          return (
            <FilePreview
              filePath={editorData.filePath}
              content={editorData.content}
              size={editorData.size}
              containerTaskId={editorData.containerTaskId}
            />
          )
        }

        return (
          <CodeEditor
            filePath={editorData.filePath}
            content={editorData.content}
            size={editorData.size}
            onSave={handleSaveFile}
            tabId={tab.id}
          />
        )
      }

      // 文件审批标签渲染
      if (tab.moduleId === '__file_review__') {
        // BUG-FIX-fix_20260625_workspace_tabs_persist:
        // 历史遗留分支：交互附带文件不再创建 __file_review__ 类型 Tab，
        // 现在统一走 __file_editor__。此处只做兼容旧持久化数据，显示提示让用户关闭。
        return (
          <div className="flex h-full flex-col items-center justify-center p-4">
            <div className="text-muted-foreground text-sm">此审阅 Tab 已过期，请关闭</div>
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
                    refreshKey={workspaceRefreshKeyRef.current}
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
           * 加载文件内容并注册为文件编辑器 Tab，在工作区中以 CodeEditor 或 FilePreview 组件展示。
           *
           * @param filePath - 文件相对路径（如 src/main.py）
           * @param fileName - 文件名（如 main.py）
           */
          const handleFileClick = async (filePath: string, fileName: string) => {
            const containerId = tab.dataSource?.replace('workspace://', '') || ''
            if (!containerId) return

            const tabId = `file-${containerId}-${filePath.replace(/[/\\]/g, '_')}`
            const layoutStore = useLayoutModeStore.getState()

            // 如果 Tab 已存在，直接激活
            const existingTab = layoutStore.workspaceTabs.find(t => t.id === tabId)
            if (existingTab) {
              layoutStore.setActiveTab(tabId)
              return
            }

            try {
              const resp = await apiClient.get(`/api/v1/workspaces/${containerId}/file-content`, {
                params: { path: filePath }
              })
              if (resp.data?.success) {
                registerFileEditor(tabId, {
                  filePath,
                  fileName,
                  content: resp.data.content ?? '',
                  size: resp.data.size,
                  containerTaskId: containerId,
                })
                layoutStore.addWorkspaceTab({
                  id: tabId,
                  title: fileName,
                  icon: '📄',
                  moduleId: '__file_editor__',
                  isActive: true,
                  isPinned: false,
                })
              }
            } catch {
              // 静默失败
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
                  refreshKey={workspaceRefreshKeyRef.current}
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
            {isMobile ? (
              <div className="flex min-h-0 flex-1 overflow-hidden">
                <section className="flex flex-1 flex-col overflow-hidden">
                  {chatContent}
                </section>
              </div>
            ) : (
              <Splitter
                layout="horizontal"
                className="min-h-0 flex-1 overflow-hidden"
                onCollapse={(collapsed) => {
                  if (collapsed[1] !== workspaceCollapsed) {
                    toggleWorkspace()
                  }
                }}
              >
                {/* Chat Panel */}
                <Splitter.Panel
                  defaultSize={resolved.chatPanel.width}
                  min={resolved.chatPanel.minWidth}
                >
                  <div className="border-border h-full overflow-hidden border-r">
                    {chatContent}
                  </div>
                </Splitter.Panel>
                {/* Workspace Panel */}
                <Splitter.Panel
                  collapsible
                  min={resolved.workspacePanel.minWidth}
                >
                  <section className="h-full min-w-0 overflow-hidden">
                    <WorkspacePanel
                      tabs={workspaceTabs}
                      onTabChange={setActiveTab}
                      onTabClose={handleCloseTab}
                      renderTabContent={renderTabContent}
                      onFullscreen={toggleWorkspaceFullscreen}
                      isFullscreen={false}
                    />
                  </section>
                </Splitter.Panel>
              </Splitter>
            )}
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
