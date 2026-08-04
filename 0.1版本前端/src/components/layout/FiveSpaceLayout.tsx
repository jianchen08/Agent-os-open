/** Five Space Layout Component Implements the five-rendering-space layout: */

import { Minimize2, FolderOpen } from 'lucide-react'
import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react'
import { getEditorForFile } from '@/config/fileEditors'
import { cn } from '@/lib/utils'
import { Splitter } from 'antd'
import apiClient from '@/services/api/client'
import { safeLoadLayout, resolveLayout } from '@/services/layout/resolver'
import type { LayoutConfig } from '@/types/layout'
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

/** Get viewport breakpoint from width */
function getBreakpoint(
  width: number,
  breakpoints: { mobile: number; tablet: number; desktop: number; widescreen: number },
): ViewportBreakpoint {
  if (width < breakpoints.mobile) return 'mobile'
  if (width < breakpoints.tablet) return 'tablet'
  if (width < breakpoints.desktop) return 'desktop'
  return 'widescreen'
}

/** Five Space Layout Component Arranges the UI into five rendering spaces: */
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
  const workspacePanelRatio = useUIStore((s) => s.workspacePanelRatio)
  const setWorkspacePanelRatio = useUIStore((s) => s.setWorkspacePanelRatio)
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
  const visitedTabIds = useLayoutModeStore((s) => s.visitedTabIds)
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

  /** 工作区刷新 key，用于驱动 FileTreeWidget 等组件重新加载。
   * 直接作为 renderTabContent 的依赖传入任务树：任务状态事件 bump workspaceDataVersion
   * → 此处重算新字符串 → renderTabContent 闭包捕获新值 → 任务树收到新 refreshKey 重取。
   * CodeEditor 不受影响：WorkspacePanel 用 key=tab.id（稳定），CodeEditor props 不变，
   * React 复用同一实例，内部 state 保留，不会因 callback identity 变化而 remount。 */
  const workspaceRefreshKey = useMemo(
    () => `${connectionStatus?.lastConnectedAt ?? ''}-v${workspaceDataVersion}`,
    [connectionStatus?.lastConnectedAt, workspaceDataVersion],
  )

  /** 文件编辑器自动刷新逻辑 每 3 秒轮询检查已打开的文件编辑器 Tab 对应的文件是否被外部修改， */
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

  /** 处理工作区 Tab 关闭，清理 fileEditorRegistry 中对应的文件内容缓存 */
  const handleCloseTab = useCallback((tabId: string) => {
    const tab = useLayoutModeStore.getState().workspaceTabs.find(t => t.id === tabId)
    if (tab?.moduleId === '__file_editor__') {
      removeFileEditorData(tabId)
    }
    closeWorkspaceTab(tabId)
  }, [closeWorkspaceTab])

  // Layout resolution
  // themeConfig 异步解析（主题从 store/API 加载），刷新后会在首帧后才就位。
  // 若 layoutConfig 直接依赖 themeConfig，则 resolved 会随 themeConfig 到达而重算，
  // 导致已渲染的面板像素宽度被覆盖（Splitter.Panel 的 size 是受控的）→ 面板宽度跳动。
  // 修复：首次解析出有效 layoutConfig 后冻结，之后 themeConfig 变化不再重算面板宽度。
  // themeConfig 的布局字段基本是静态的（min/max/default 宽度），无需跟随重算；
  // 面板宽度只在用户主动操作（拖拽改 ratio）或窗口 resize 时变。
  const themeLayoutRaw = (themeConfig as any)?.layout
  const frozenLayoutRef = useRef<LayoutConfig | null>(null)
  if (!frozenLayoutRef.current) {
    frozenLayoutRef.current = safeLoadLayout(themeLayoutRaw)
  }
  const layoutConfig = frozenLayoutRef.current
  const resolved = useMemo(
    () => resolveLayout(layoutConfig, viewportWidth, workspacePanelRatio ?? undefined),
    [layoutConfig, viewportWidth, workspacePanelRatio],
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
    if (!mobileInitRef.current && isMobile) {
      const currentCollapsed = useUIStore.getState().sidebarCollapsed
      if (!currentCollapsed) {
        mobileInitRef.current = true
        useUIStore.getState().setSidebarCollapsed(true)
      }
    }
  }, [isMobile])

  const toggleWorkspaceFullscreen = useCallback(() => setWorkspaceFullscreen((prev) => !prev), [])

  /** 处理任务树节点点击（对话按钮）。 通过全局管道导航服务（pipelineNavigator）实现跨会话跳转： */
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

 /** 渲染工作区 Tab 内容 连接 Schema 渲染链路 */
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
              url={editorData.url}
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
              url={editorData.url}
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
        // 兼容旧持久化数据：__file_review__ Tab 已统一为 __file_editor__，此处提示用户关闭。
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
          /** 处理工作空间文件树中的文件点击 加载文件内容并注册为文件编辑器 Tab，在工作区中以 CodeEditor 或 FilePreview 组件展示。 */
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
              visitedTabIds={visitedTabIds}
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
                onResizeEnd={(sizes) => {
                  // sizes 为像素数组 [chatPx, workspacePx]；换算成工作区比例并持久化
                  const total = sizes[0] + sizes[1]
                  if (!total || !Number.isFinite(total)) return
                  let ratio = sizes[1] / total
                  // 夹到合理区间，避免越界或低于最小宽度对应的极端比例
                  const minChat = resolved.chatPanel.minWidth
                  const minWorkspace = resolved.workspacePanel.minWidth
                  const minRatio = minWorkspace / total
                  const maxRatio = 1 - minChat / total
                  ratio = Math.min(Math.max(ratio, minRatio), maxRatio)
                  setWorkspacePanelRatio(ratio)
                }}
              >
                {/* Chat Panel */}
                <Splitter.Panel
                  size={resolved.chatPanel.width}
                  min={resolved.chatPanel.minWidth}
                >
                  <div className="border-border h-full overflow-hidden border-r">
                    {chatContent}
                  </div>
                </Splitter.Panel>
                {/* Workspace Panel */}
                <Splitter.Panel
                  collapsible
                  size={resolved.workspacePanel.width}
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
                      visitedTabIds={visitedTabIds}
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
                  visitedTabIds={visitedTabIds}
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
