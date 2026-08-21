/** Five Space Layout Component Implements the five-rendering-space layout: */

import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { FolderOpen, Menu, Minimize2, PanelRightIcon } from '@/assets/icons'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { getEditorForFile } from '@/config/fileEditors'
// 按需引入 antd Splitter 子模块，避免加载 antd 全量入口（26+ 组件 → 全部 icons →
// 触发 847 项 @ant-design/icons-svg/lib/asn/* 全量预构建，首屏 JS 与启动预构建时间双高）
import apiClient from '@/services/api/client'
import { safeLoadLayout } from '@/services/layout/resolver'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { getFileEditorData, registerFileEditor, removeFileEditorData, updateFileEditorData, emitFileChange } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThemeStore } from '@/stores/themeStore'
import { cn } from '@/lib/utils'
import { useUIStore } from '@/stores/uiStore'
import { AlertBanner, useLayoutAlerts, type AlertBannerItem } from './AlertBanner'
import { FloatingWindowManager, renderFloatingWindowContent } from './FloatingWindowManager'
import { FullscreenOverlay } from './FullscreenOverlay'
import { WorkspaceHost } from './WorkspaceHost'
import { CodeEditor } from '../workspace/CodeEditor'
import { FilePreview } from '../workspace/FilePreview'
import type { LayoutConfig, FloatingWindowInstance, WorkspaceTab  } from '@/types/layout'
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

/** 移动端/桌面两档断点分界（平板=触屏桌面，不单独设计形态）。
 * 值取布局配置 mobile 断点（默认 768px，可被主题覆盖）。 */
function isMobileViewport(width: number, mobileBreakpoint: number): boolean {
  return width < mobileBreakpoint
}

/** Five Space Layout Component Arranges the UI into five rendering spaces: */
export function FiveSpaceLayout({
  chatContent,
  sidebarContent,
  showThemePanel: _showThemePanel = false,
  onShowThemePanel: _onShowThemePanel,
  onLogout: _onLogout,
}: FiveSpaceLayoutProps) {
  const themeConfig = useThemeStore((s) => s.themeConfig)
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const workspaceCollapsed = useUIStore((s) => s.workspaceCollapsed)
  const setWorkspaceCollapsed = useUIStore((s) => s.setWorkspaceCollapsed)
  const workspaceMaximized = useUIStore((s) => s.workspaceMaximized)
  const setWorkspaceMaximized = useUIStore((s) => s.setWorkspaceMaximized)
  // 本地拖动百分比（受控 size 必须在 onResize 中更新，否则会弹回导致“拖不动”）

  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const [workspaceFullscreen, setWorkspaceFullscreen] = useState(false)
  /** 移动端工作区覆盖层是否打开 */
  const [mobileWorkspaceOpen, setMobileWorkspaceOpen] = useState(false)
  const [viewportWidth, setViewportWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth : 1280,
  )
  const navigate = useNavigate()

  // Store state
  const floatingWindows = useLayoutModeStore((s) => s.floatingWindows)
  const workspaceTabs = useLayoutModeStore((s) => s.workspaceTabs)
  const visitedTabIds = useLayoutModeStore((s) => s.visitedTabIds)
  const fullscreenActive = useLayoutModeStore((s) => s.fullscreenActive)
  const fullscreenTitle = useLayoutModeStore((s) => s.fullscreenTitle)
  const fullscreenContent = useLayoutModeStore((s) => s.fullscreenContent)
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
            `/ext/channel_api/workspaces/${editorData.containerTaskId}/file-content`,
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
    // 钉住页签不允许关闭（工作区默认页）
    if (tab?.isPinned) return
    if (tab?.moduleId === '__file_editor__') {
      removeFileEditorData(tabId)
    }
    closeWorkspaceTab(tabId)
  }, [closeWorkspaceTab])

  // Layout resolution
  // themeConfig 异步解析（主题从 store/API 加载），刷新后会在首帧后才就位。
  // 若 layoutConfig 直接依赖 themeConfig，则 resolved 会随 themeConfig 到达而重算，
  // 导致已渲染的面板像素宽度被覆盖（Splitter.Panel 的 size 是受控的）→ 面板宽度跳动。
  // 首次解析出有效 layoutConfig 后冻结，之后 themeConfig 变化不再重算面板宽度。
  // themeConfig 的布局字段基本是静态的（min/max/default 宽度），无需跟随重算；
  // 面板宽度只在用户主动操作（拖拽改 ratio）或窗口 resize 时变。
  const themeLayoutRaw = (themeConfig as any)?.layout
  const frozenLayoutRef = useRef<LayoutConfig | null>(null)
  if (!frozenLayoutRef.current) {
    frozenLayoutRef.current = safeLoadLayout(themeLayoutRaw)
  }
  const layoutConfig = frozenLayoutRef.current
  // 两档断点：< mobile（768px）移动形态；≥ mobile 桌面/平板（触屏桌面）同一形态
  const isMobile = isMobileViewport(viewportWidth, layoutConfig.breakpoints.mobile)

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


  /** 异常提示条点击：连接 → 打开监控面板；预算 → 成本看板；审批 → 审批弹窗全局可见，无需跳转 */
  const handleAlertAction = useCallback(
    (item: AlertBannerItem) => {
      if (item.kind === 'connection') {
        // 监控页已声明化（monitoring 插件 contributes.pages path /monitoring）——直接打开
        const opened = openWorkspacePanelByPath('/monitoring')
        if (!opened) navigate('/monitoring')
      }
      if (item.kind === 'budget') {
        // 成本看板已声明化（cost_control 插件 contributes.pages path /cost）——直接打开
        const opened = openWorkspacePanelByPath('/cost')
        if (!opened) navigate('/cost')
      }
    },
    [navigate],
  )
  const layoutAlerts = useLayoutAlerts()

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
              `/ext/channel_api/workspaces/${containerId}/file-content`,
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

      // 顶栏打开的内置面板：优先走 component 注册的 widget
      if (tab.component && (tab.moduleId?.startsWith('__panel_') || tab.moduleId?.startsWith('__builtin_'))) {
        // 降级映射命中的渲染打 data-fallback 标记（声明↔注册断链可排查，FE3）
        const panelExact = widgetRegistry.get(tab.component)
        const WidgetComponent = panelExact ?? widgetRegistry.findFallback(tab.component)
        if (WidgetComponent) {
          return (
            <div className="h-full min-h-0 overflow-hidden" data-fallback={panelExact ? undefined : tab.component}>
              <WidgetComponent panel={tab.component} dataSource={tab.dataSource} />
            </div>
          )
        }
      }

      // component-based 渲染路径：通过 tab.component 直接查找 widget
      if (tab.component) {
        const componentExact = widgetRegistry.get(tab.component)
        const WidgetComponent = componentExact ?? widgetRegistry.findFallback(tab.component)
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
              const resp = await apiClient.get(`/ext/channel_api/workspaces/${containerId}/file-content`, {
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
              await apiClient.post(`/ext/channel_api/workspaces/${containerId}/open`)
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
              <div
                className="h-full overflow-auto p-2 sm:p-4"
                data-fallback={componentExact ? undefined : tab.component}
              >
                <WidgetComponent
                  {...(tab.props ?? {})}
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

  // Render floating window content：接通 PageRenderer（widget/schema 分发），
  // 兜底兼容旧 FloatingWindowInstance（widgetRegistry）与占位。
  const renderFloatingContent = useCallback(
    (window: FloatingWindowInstance) => renderFloatingWindowContent(window),
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
        } else if (workspaceMaximized) {
          setWorkspaceMaximized(false)
        } else if (mobileWorkspaceOpen) {
          setMobileWorkspaceOpen(false)
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [fullscreenActive, exitFullscreen, workspaceFullscreen, workspaceMaximized, setWorkspaceMaximized, mobileWorkspaceOpen])

  return (
    <div
      className="bg-background text-foreground flex w-screen flex-col overflow-hidden"
      style={{ fontFamily: 'var(--font-ui, var(--font-family))', height: '100dvh' }}
    >
      {workspaceFullscreen ? (
        // 全屏模式：工作区 100% 占满视口，不渲染任何标题条。
        // 退出入口 = 工作区 Tab 栏的全屏按钮(isFullscreen 时显示退出图标)或 ESC 键。
        <div className="min-h-0 flex-1 overflow-hidden">
          <WorkspaceHost
            tabs={workspaceTabs}
            onTabChange={setActiveTab}
            onTabClose={handleCloseTab}
            renderTabContent={renderTabContent}
            onFullscreen={toggleWorkspaceFullscreen}
            isFullscreen={true}
            visitedTabIds={visitedTabIds}
          />
        </div>
      ) : workspaceMaximized ? (
        <>
          {/* 最大化模式：工作区 100% 占满（顶栏已移除，2026-08-21 布局简化） */}
          <AlertBanner alerts={layoutAlerts} onAction={handleAlertAction} />
          <div className="min-h-0 flex-1 overflow-hidden">
            <WorkspaceHost
              tabs={workspaceTabs}
              onTabChange={setActiveTab}
              onTabClose={handleCloseTab}
              renderTabContent={renderTabContent}
              onFullscreen={toggleWorkspaceFullscreen}
              isFullscreen={false}
              visitedTabIds={visitedTabIds}
            />
          </div>
        </>
      ) : (
        <>
          {/* ---- Top Navigation Bar (shared AppHeader) ---- */}

          {/* ---- 异常浮现提示条（无常驻底栏；连接断开/审批待处理时出现） ---- */}
          <AlertBanner alerts={layoutAlerts} onAction={handleAlertAction} />

          {/* ---- Main Content Area ---- */}
          <div className="relative flex min-h-0 flex-1 overflow-hidden">
            {(isMobile || sidebarCollapsed) && (
              <button
                type="button"
                onClick={() => useUIStore.getState().setSidebarCollapsed(false)}
                className="text-muted-foreground hover:bg-accent hover:text-foreground absolute left-1 top-2 z-10 flex h-7 w-7 items-center justify-center rounded-md transition-colors"
                title="展开侧边栏"
                aria-label="展开侧边栏"
                data-testid="sidebar-expand-float"
              >
                <Menu className="h-4 w-4" />
              </button>
            )}
            {/* 移动端侧边栏：抽屉，隐藏时完全不占位 */}
            {sidebarContent && isMobile && !sidebarCollapsed && (
              <div
                className="fixed inset-0 z-40"
                style={{ top: 'var(--layout-titlebar-height, 32px)' }}
                data-testid="mobile-sidebar-drawer"
              >
                <div
                  className="absolute inset-0 bg-[var(--overlay-bg)]"
                  onClick={() => useUIStore.getState().setSidebarCollapsed(true)}
                  data-testid="mobile-sidebar-backdrop"
                />
                <aside
                  className="absolute bottom-0 left-0 top-0 z-50 flex w-[78%] max-w-[320px] flex-col shadow-xl safe-area-pb"
                  style={{ background: 'var(--ds-bg-panel, hsl(var(--card)))' }}
                >
                  <div className="min-h-0 flex-1 overflow-hidden">{sidebarContent}</div>
                  <div className="flex items-center gap-1 px-2 py-1.5">
                    <button
                      type="button"
                      onClick={() => setMobileWorkspaceOpen(true)}
                      className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-7 w-7 items-center justify-center rounded-md transition-colors"
                      title="工作区"
                      aria-label="工作区"
                      data-testid="mobile-workspace-btn"
                    >
                      <PanelRightIcon className="h-4 w-4" />
                    </button>
                  </div>
                </aside>
              </div>
            )}

            {isMobile ? (
              <div className="flex min-h-0 flex-1 overflow-hidden">
                <section className="flex flex-1 flex-col overflow-hidden" data-region="chat">
                  {chatContent}
                </section>
              </div>
            ) : (
              /* 桌面（布局 v3，用户裁决 2026-08-21）：聊天全宽常驻；侧栏/工作区为
                 浮出面板（左上角恒定图标组切换，无遮罩不阻断聊天交互）；
                 全界面无分割线（浮层自带阴影边界）。 */
              <section className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-region="chat">
                {/* 左上角常驻图标组（位置恒定：侧栏 / 工作区） */}
                <div className="absolute left-2 top-2 z-30 flex items-center gap-1 rounded-lg p-0.5">
                  <button
                    type="button"
                    onClick={() => useUIStore.getState().setSidebarCollapsed(!sidebarCollapsed)}
                    className={cn(
                      'flex h-7 w-7 items-center justify-center rounded-md transition-colors',
                      !sidebarCollapsed && sidebarContent
                        ? 'bg-accent text-foreground'
                        : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                    )}
                    title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
                    aria-label="侧边栏"
                    data-testid="sidebar-toggle-float"
                  >
                    <Menu className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setWorkspaceCollapsed(!workspaceCollapsed)}
                    className={cn(
                      'flex h-7 w-7 items-center justify-center rounded-md transition-colors',
                      !workspaceCollapsed
                        ? 'bg-accent text-foreground'
                        : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                    )}
                    title={workspaceCollapsed ? '展开工作区' : '收起工作区'}
                    aria-label="工作区"
                    data-testid="workspace-toggle-float"
                  >
                    <PanelRightIcon className="h-4 w-4" />
                  </button>
                </div>

                {chatContent}

                {/* 侧栏浮出面板（左）：主题侧栏样式（--ds-bg-panel） */}
                {sidebarContent && !sidebarCollapsed && (
                  <aside
                    className="absolute inset-y-0 left-0 z-20 flex w-[248px] min-w-[200px] max-w-[320px] flex-col shadow-2xl"
                    style={{ background: 'var(--ds-bg-panel, hsl(var(--card)))' }}
                    data-testid="sidebar-panel"
                    data-region="sidebar"
                  >
                    <div className="min-h-0 flex-1 overflow-hidden">{sidebarContent}</div>
                  </aside>
                )}

                {/* 工作区浮出面板（右）：主题面板样式 */}
                {!workspaceCollapsed && (
                  <div
                    className="absolute inset-y-0 right-0 z-20 flex w-[46%] min-w-[380px] flex-col shadow-2xl"
                    style={{ background: 'var(--ds-bg-elevated, hsl(var(--card)))' }}
                    data-region="workspace"
                  >
                    <WorkspaceHost
                      tabs={workspaceTabs}
                      onTabChange={setActiveTab}
                      onTabClose={handleCloseTab}
                      renderTabContent={renderTabContent}
                      onFullscreen={toggleWorkspaceFullscreen}
                      isFullscreen={false}
                      visitedTabIds={visitedTabIds}
                    />
                  </div>
                )}
              </section>
            )}
          </div>

          {/* ---- StatusBar 已移除（task_layout_responsive 任务 2：无常驻底栏，
               状态信息并入各区——连接小圆点在顶栏、成本在输入框、插件项在侧栏底部） ---- */}

          {/* 移动端工作区全屏覆盖层 */}
          {isMobile && mobileWorkspaceOpen && (
            <div
              className="fixed inset-0 z-30 flex flex-col bg-background"
              style={{ top: 'var(--layout-titlebar-height, 32px)' }}
              data-testid="mobile-workspace-overlay"
            >
              {/* 工作区顶部操作栏 */}
              <div className="border-border flex h-9 shrink-0 items-center justify-between border-b px-2">
                <span className="text-foreground text-xs font-medium">面板</span>
                <button
                  onClick={() => setMobileWorkspaceOpen(false)}
                  className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors"
                  title="返回对话"
                  data-testid="mobile-workspace-back"
                >
                  <Minimize2 className="h-3.5 w-3.5" />
                  <span>返回对话</span>
                </button>
              </div>
              {/* 工作区内容 */}
              <div className="safe-area-pb min-h-0 flex-1 overflow-hidden">
                <WorkspaceHost
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
          isMobile={isMobile}
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
