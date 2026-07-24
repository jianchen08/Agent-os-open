/**
 * ChatPanelShell — ADR §五 ChatPanel 外壳内核固定布局
 *
 * 实现 ADR 布局模型：
 * - ChatPanel 外壳内核固定，不可被插件移除或替换
 * - ActivityBar/SideBar/WorkspacePanel/StatusBar 为 contributes 驱动
 * - 保留 FiveSpaceLayout 的所有功能（workspace tabs、floating windows、dock bar、fullscreen）
 *
 * 布局结构：
 * ```
 * ┌──────────────────────────────────────────────────────┐
 * │ AppHeader (TitleBar)                        [内核固定] │
 * ├────┬─────────────────────┬───────────────────────────┤
 * │Act │   ChatPanel         │   WorkspacePanel          │
 * │ivity│  [内核固定核心]      │   (一组可切换 tab,         │
 * │Bar │   位置不可替换        │    可分屏 / 拖拽布局)      │
 * │(图 │   ┌───────────────┐ │   ←contributes            │
 * │标条)│   │ 内容可变:      │ │    .workspaceTabs        │
 * │ ←co│   │ • 消息卡片     │ │   (终端 / 预览 / 审批 /    │
 * │ntri│   │   .chatMessages│ │    编辑 / 任意插件 tab)   │
 * │buti│   │ • 交互模式     │ │                           │
 * │ons.│   │   .chatInter-  │ │                           │
 * │view│   │   actions      │ │                           │
 * │sCon│   │ • 输入区动作   │ │                           │
 * │tain│   │   .chatActions │ │                           │
 * │ers │   └───────────────┘ │                           │
 * ├────┴─────────────────────┴───────────────────────────┤
 * │ StatusBar ←contributes.statusBarItems                 │
 * └──────────────────────────────────────────────────────┘
 * ```
 */

import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react'
import { Minimize2, FolderOpen } from '@/assets/icons'
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

/** ChatPanelShell 属性（与 FiveSpaceLayout 兼容） */
export interface ChatPanelShellProps {
  chatContent: React.ReactNode
  sidebarContent?: React.ReactNode
  onToggleMode?: () => void
  showThemePanel?: boolean
  onShowThemePanel?: (show: boolean) => void
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

/**
 * ChatPanelShell — ADR §五 布局模型实现
 *
 * 与 FiveSpaceLayout 完全兼容的替代方案，实现 ChatPanel 外壳内核固定。
 */
export function ChatPanelShell({
  chatContent,
  sidebarContent,
  onToggleMode,
  showThemePanel = false,
  onShowThemePanel,
  onLogout,
}: ChatPanelShellProps) {
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

  const workspaceRefreshKey = useMemo(
    () => `${connectionStatus?.lastConnectedAt ?? ''}-v${workspaceDataVersion}`,
    [connectionStatus?.lastConnectedAt, workspaceDataVersion],
  )

  // 文件编辑器自动刷新
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
            if (newContent !== editorData.content) {
              updateFileEditorData(tab.id, {
                content: newContent,
                size: newSize,
              })
              emitFileChange(tab.id)
            }
          }
        } catch {
          // 静默失败
        }
      }
    }, intervalMs)

    return () => clearInterval(timer)
  }, [])

  // 响应式
  useEffect(() => {
    const handleResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  const isMobile = viewportWidth < 768
  const layoutConfig = safeLoadLayout()
  const breakpoint = getBreakpoint(viewportWidth, layoutConfig.breakpoints)
  const resolved = useMemo(() => resolveLayout(layoutConfig, breakpoint, {
    sidebarCollapsed,
    workspaceCollapsed,
    workspacePanelRatio,
  }), [layoutConfig, breakpoint, sidebarCollapsed, workspaceCollapsed, workspacePanelRatio])

  const mobileInitRef = useRef(false)
  useEffect(() => {
    if (isMobile) {
      if (!mobileInitRef.current) {
        mobileInitRef.current = true
        useUIStore.getState().setSidebarCollapsed(true)
      }
    }
  }, [isMobile])

  const toggleWorkspaceFullscreen = useCallback(() => setWorkspaceFullscreen((prev) => !prev), [])

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

  const handleCloseTab = useCallback((tabId: string) => {
    removeFileEditorData(tabId)
    closeWorkspaceTab(tabId)
  }, [closeWorkspaceTab])

  const renderTabContent = useCallback(
    (tab: WorkspaceTab) => {
      if (tab.moduleId === '__file_editor__') {
        const editorData = getFileEditorData(tab.id)
        if (!editorData) {
          return (
            <div className="flex h-full flex-col items-center justify-center p-4">
              <div className="text-muted-foreground text-sm">文件数据已过期</div>
            </div>
          )
        }

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
              onContentChange={(newContent) => {
                updateFileEditorData(tab.id, { content: newContent })
              }}
            />
          )
        }

        if (editor.id === 'html_preview') {
          return (
            <HtmlPreviewWidget
              content={editorData.content}
              title={editorData.filePath}
            />
          )
        }

        return (
          <CodeEditor
            filePath={editorData.filePath}
            content={editorData.content}
            onSave={handleSaveFile}
            onContentChange={(newContent) => {
              updateFileEditorData(tab.id, { content: newContent })
            }}
          />
        )
      }

      // Schema-driven widget rendering
      const module = schemaRegistry.get(tab.moduleId ?? '')
      if (!module) {
        return (
          <div className="flex h-full flex-col items-center justify-center p-4">
            <div className="text-muted-foreground mb-2 text-sm">{tab.title}</div>
            <div className="text-muted-foreground/60 text-xs">模块未注册</div>
          </div>
        )
      }

      const widgetName = tab.widget || tab.moduleId
      const WidgetComponent = widgetRegistry.get(widgetName ?? '')?.component
      if (!WidgetComponent) {
        return (
          <div className="flex h-full flex-col items-center justify-center p-4">
            <div className="text-muted-foreground mb-2 text-sm">{tab.title}</div>
            <div className="text-muted-foreground/60 text-xs">Widget 未注册: {widgetName}</div>
          </div>
        )
      }

      // 特殊处理：文件树
      if (widgetName === 'file_tree' || widgetName === 'tree') {
        const handleFileClick = async (node: Record<string, unknown>) => {
          const filePath = node.path as string
          if (!filePath) return
          const containerId = tab.dataSource?.replace('workspace://', '') || ''
          if (!containerId) return

          const tabId = `file_editor_${Date.now()}`
          const fileName = filePath.split('/').pop() || filePath

          try {
            const resp = await apiClient.get(
              `/api/v1/workspaces/${containerId}/file-content`,
              { params: { path: filePath } }
            )
            if (resp.data?.success) {
              registerFileEditor(tabId, {
                filePath,
                fileName,
                content: resp.data.content ?? '',
                size: resp.data.size,
                containerTaskId: containerId,
              })
              useLayoutModeStore.getState().addWorkspaceTab({
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

        const folderContainerId = tab.dataSource?.replace('workspace://', '') || ''

        return (
          <div className="relative h-full">
            {folderContainerId && (
              <button
                className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md border bg-background/80 px-2 py-1 text-xs text-muted-foreground backdrop-blur-sm transition-colors hover:bg-accent hover:text-foreground"
                onClick={async () => {
                  try {
                    await apiClient.post(`/api/v1/workspaces/${folderContainerId}/open`)
                  } catch {
                    // 静默失败
                  }
                }}
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

      return (
        <div className="flex h-full flex-col items-center justify-center p-4">
          <div className="text-muted-foreground mb-2 text-sm">{tab.title}</div>
          <div className="text-muted-foreground/60 text-xs">模块内容不可用</div>
        </div>
      )
    },
    [activeSessionId, handleTaskNodeClick, workspaceRefreshKey],
  )

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

  // ESC 键处理
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
          {/* ---- AppHeader (TitleBar) — 内核固定 ---- */}
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

          {/* ---- 主体区域：SideBar + ChatPanel + WorkspacePanel ---- */}
          <div className="relative flex min-h-0 flex-1 overflow-hidden">
            {/* 移动端侧边栏 */}
            {sidebarContent && isMobile && !sidebarCollapsed && (
              <div className="fixed inset-0 z-40" style={{ top: '2.5rem' }}>
                <div
                  className="absolute inset-0 bg-black/50"
                  onClick={() => useUIStore.getState().setSidebarCollapsed(true)}
                />
                <aside className="absolute left-0 top-0 bottom-0 z-50 flex w-72 flex-col border-r bg-background shadow-xl">
                  {sidebarContent}
                </aside>
              </div>
            )}

            {/* 桌面端侧边栏 */}
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

            {/* ChatPanel — 外壳内核固定，不可移除 */}
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
                  const total = sizes[0] + sizes[1]
                  if (!total || !Number.isFinite(total)) return
                  let ratio = sizes[1] / total
                  const minChat = resolved.chatPanel.minWidth
                  const minWorkspace = resolved.workspacePanel.minWidth
                  const minRatio = minWorkspace / total
                  const maxRatio = 1 - minChat / total
                  ratio = Math.min(Math.max(ratio, minRatio), maxRatio)
                  setWorkspacePanelRatio(ratio)
                }}
              >
                {/* Chat Panel — 内核固定 */}
                <Splitter.Panel
                  size={resolved.chatPanel.width}
                  min={resolved.chatPanel.minWidth}
                >
                  <div className="border-border h-full overflow-hidden border-r">
                    {chatContent}
                  </div>
                </Splitter.Panel>
                {/* Workspace Panel — contributes 驱动 */}
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

          {/* ---- DockBar (StatusBar) ---- */}
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

      {/* ---- Floating Windows ---- */}
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

/**
 * ChatPanel 内容插槽标识
 *
 * ADR §5.4：ChatPanel 内容可变，三个变化维度。
 * 插件通过 contributes 注册这些插槽的内容。
 */
export const CHAT_SLOTS = {
  MESSAGES: '.chatMessages',
  INTERACTIONS: '.chatInteractions',
  ACTIONS: '.chatActions',
} as const

export type ChatSlotType = (typeof CHAT_SLOTS)[keyof typeof CHAT_SLOTS]
