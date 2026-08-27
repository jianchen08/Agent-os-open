/** Five Space Layout Component Implements the five-rendering-space layout: */

import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { FolderOpen, Menu, Minimize2, PanelRightIcon } from '@/assets/icons'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { getEditorForFile } from '@/config/fileEditors'
// 按需引入 antd Splitter 子模块，避免加载 antd 全量入口（26+ 组件 → 全部 icons →
// 触发 847 项 @ant-design/icons-svg/lib/asn/* 全量预构建，首屏 JS 与启动预构建时间双高）
import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS as W } from '@/services/api/endpoints.generated'
import { safeLoadLayout } from '@/services/layout/resolver'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { getFileEditorData, registerFileEditor, removeFileEditorData, updateFileEditorData, emitFileChange } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionStore } from '@/stores/sessionStore'
import { cn } from '@/lib/utils'
import { useUIStore } from '@/stores/uiStore'
import { AlertBanner, useLayoutAlerts, type AlertBannerItem } from './AlertBanner'
import { FloatingWindowManager, renderFloatingWindowContent } from './FloatingWindowManager'
import { FullscreenOverlay } from './FullscreenOverlay'
import { WorkspaceHost } from './WorkspaceHost'
import { CodeEditor } from '../workspace/CodeEditor'
import { FilePreview } from '../workspace/FilePreview'
import type { WorkspaceTab  } from '@/types/layout'
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
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const workspaceCollapsed = useUIStore((s) => s.workspaceCollapsed)
  const setWorkspaceCollapsed = useUIStore((s) => s.setWorkspaceCollapsed)
  // 面板宽度比例（0~1，相对主内容区；null = 默认宽度）——拖拽手柄写入，
  // 持久化在 uiStorage（刷新后恢复）
  const sidebarRatio = useUIStore((s) => s.sidebarRatio)
  const workspacePanelRatio = useUIStore((s) => s.workspacePanelRatio)
  const setSidebarRatio = useUIStore((s) => s.setSidebarRatio)
  const setWorkspacePanelRatio = useUIStore((s) => s.setWorkspacePanelRatio)

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
            W.workspaces_file_content_get.replace('{container_task_id}', editorData.containerTaskId),
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

  // Layout：ThemeConfig 不承载布局域（预设/校验产物均无 layout 字段），布局恒为
  // 内置默认值（面板宽度只随用户拖拽 ratio 或窗口 resize 变，与主题加载时序无关）。
  const layoutConfig = safeLoadLayout(undefined)
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

  /** 面板拖拽调宽（左右侧边栏可拖动长短）。
   * 手柄按下后监听 pointermove 计算新宽度比例（相对主内容区），
   * 实时写入 uiStore（持久化）；抬起释放。
   * clamp 作用于各自最终比例：侧栏=光标位置比；工作区=行宽减光标
   * （右侧往左拖=增宽）。clamp 必须作用于最终比例而非原始光标比——
   * 工作区经 1-ratio 反转后，clamp 原始比会把手柄位置恒钳在 0.5 → 拖不动。 */
  const startPanelDrag = useCallback(
    (side: 'sidebar' | 'workspace') => (e: React.PointerEvent) => {
      e.preventDefault()
      const container = (e.currentTarget as HTMLElement).parentElement
      if (!container) return
      const rect = container.getBoundingClientRect()
      const onMove = (ev: PointerEvent) => {
        const raw = (ev.clientX - rect.left) / rect.width
        if (side === 'sidebar') {
          setSidebarRatio(Math.min(0.5, Math.max(0.12, raw)))
        } else {
          setWorkspacePanelRatio(Math.min(0.5, Math.max(0.15, 1 - raw)))
        }
      }
      const onUp = () => {
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
    },
    [setSidebarRatio, setWorkspacePanelRatio],
  )

  /** 面板宽度（px）：有持久化比例用比例 × 主内容区宽，否则默认宽度 */
  const panelWidth = useCallback(
    (side: 'sidebar' | 'workspace', defaultPx: number, minPx: number, maxPx: number) => {
      const ratio = side === 'sidebar' ? sidebarRatio : workspacePanelRatio
      if (ratio === null) return defaultPx
      const container = document.querySelector('[data-region="chat"]')
      const avail = container?.getBoundingClientRect().width ?? 1200
      return Math.min(maxPx, Math.max(minPx, Math.round(avail * ratio)))
    },
    [sidebarRatio, workspacePanelRatio],
  )

  /** 工作区默认宽度：主内容区 42%（与旧 w-[42%] 一致；容器宽变化时跟随） */
  const workspaceDefaultWidth = useMemo(() => {
    if (typeof window === 'undefined') return 504
    const container = document.querySelector('[data-region="chat"]')
    const avail = container?.getBoundingClientRect().width ?? window.innerWidth
    return Math.round(avail * 0.42)
  }, [viewportWidth, sidebarRatio, workspacePanelRatio])

  /** 工作区最大宽度：主内容区 50%（拖拽上限，防挤压聊天区） */
  const workspaceMaxWidth = useMemo(() => {
    if (typeof window === 'undefined') return 600
    const container = document.querySelector('[data-region="chat"]')
    const avail = container?.getBoundingClientRect().width ?? window.innerWidth
    return Math.max(360, Math.round(avail * 0.5))
  }, [viewportWidth, sidebarRatio, workspacePanelRatio])


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
              W.workspaces_file_content_put.replace('{container_task_id}', containerId),
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
              const resp = await apiClient.get(W.workspaces_file_content_get.replace('{container_task_id}', containerId), {
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
              const resp = await apiClient.post(
                W.workspaces_open.replace('{container_task_id}', containerId),
              )
              // 业务级失败如实透传（目录缺失/无连接器等）——后端 200 信封
              // 携带 success:false + message，静默会让用户以为打开了
              const data = resp?.data as { success?: boolean; message?: string } | undefined
              if (data && data.success === false) {
                useNotificationStore.getState().addNotification({
                  title: '打开文件夹失败',
                  message: data.message || '后端未能打开工作区目录',
                  priority: 'normal',
                  category: 'alert',
                  isBlocking: false,
                  autoDismissMs: 6000,
                  sourceLabel: '前端',
                })
              }
            } catch {
              // 传输层失败：与面板操作一致的静默降级
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

  // 浮动窗内容分发：PageRenderer 按 widget/schema 分发，未命中落占位。
  // renderFloatingWindowContent 是模块级纯函数（无 props 闭包），直接作 renderContent 传引用。
  const renderFloatingContent = renderFloatingWindowContent

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
      style={{
        fontFamily: 'var(--font-ui, var(--font-family))',
        // 皮肤装饰条槽位：仅"带文字的替代性条栏"（miku 标题栏/状态栏类，
        // skinRuntime 按文字内容判定）让位——整根下移/高度扣减；纯图形
        // 垂坠装饰（maid 花边）原生覆盖式零位移，变量恒 0 不占位
        height: 'calc(100dvh - var(--skin-chrome-top, 0px) - var(--skin-chrome-bottom, 0px))',
        paddingTop: 'var(--skin-chrome-top, 0px)',
        paddingBottom: 'var(--skin-chrome-bottom, 0px)',
      }}
    >
      {/* 单树布局：工作区全屏不再切换 JSX 分支——分支切换会让
          WorkspaceHost/ChatContainer 整棵 unmount+remount（全屏切换必重载数据、
          组件状态全丢的根因）。全屏=常驻树内 CSS 隐藏侧栏/聊天 + 工作区 flex-1，
          组件恒定性保住（React 同位置同类型即复用实例）。 */}
      {/* 异常浮现提示条（无常驻底栏；连接断开/审批待处理时出现；工作区全屏时让位） */}
      {!workspaceFullscreen && <AlertBanner alerts={layoutAlerts} onAction={handleAlertAction} />}

      {/* ---- Main Content Area ---- */}
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {(isMobile || sidebarCollapsed) && !workspaceFullscreen && (
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
              style={{ background: 'var(--region-sidebar-bg, var(--ds-bg-panel, var(--sidebar-bg, hsl(var(--card)))))' }}
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
          <div
            className={cn(
              'flex min-h-0 flex-1 overflow-hidden',
              workspaceFullscreen && 'hidden',
            )}
          >
            <section className="flex flex-1 flex-col overflow-hidden" data-region="chat">
              {chatContent}
            </section>
          </div>
        ) : (
          /* 桌面（布局 v6）：并排让位式——侧栏/工作区
             展开时聊天区让位（不遮挡），收起时聊天全宽。图标钉在页面左/右上角
             （位置恒定）；各面板自顶全高展开，顶部 40px 图标带
             归入各自展开的区域（图标落在所属区域边角内，从视觉上属于该区域），
             区域间距用位置计算让位而非移动图标；边界无边线。 */
          <section className="relative flex min-h-0 flex-1 overflow-hidden" data-region="chat">
            {/* 侧栏开关：钉在页面左上角；侧栏展开时落在侧栏区域顶角内（工作区全屏时隐藏） */}
            <button
              type="button"
              onClick={() => useUIStore.getState().setSidebarCollapsed(!sidebarCollapsed)}
              className={cn(
                'absolute left-2 top-2 z-30 flex h-7 w-7 items-center justify-center rounded-md transition-colors',
                !sidebarCollapsed && sidebarContent
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                workspaceFullscreen && 'hidden',
              )}
              title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
              aria-label="侧边栏"
              data-testid="sidebar-toggle-float"
            >
              <Menu className="h-4 w-4" />
            </button>
            {/* 工作区开关：钉在页面右上角；工作区展开时落在工作区区域顶角内（工作区全屏时隐藏） */}
            <button
              type="button"
              onClick={() => setWorkspaceCollapsed(!workspaceCollapsed)}
              className={cn(
                'absolute right-2 top-2 z-30 flex h-7 w-7 items-center justify-center rounded-md transition-colors',
                !workspaceCollapsed
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                workspaceFullscreen && 'hidden',
              )}
              title={workspaceCollapsed ? '展开工作区' : '收起工作区'}
              aria-label="工作区"
              data-testid="workspace-toggle-float"
            >
              <PanelRightIcon className="h-4 w-4" />
            </button>

            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <div className="flex min-h-0 flex-1 overflow-hidden">
                {/* 侧栏（让位式，主题面板样式，无边线；顶部让出图标带；
                    背景链与 Sidebar.tsx 内层统一（--ds-bg-panel 优先），
                    图标条与侧栏内容一色；宽度可拖拽调；
                    工作区全屏时 CSS 隐藏保挂载） */}
                {sidebarContent && !sidebarCollapsed && (
                  <aside
                    className={cn(
                      'flex shrink-0 flex-col overflow-hidden pt-10',
                      workspaceFullscreen && 'hidden',
                    )}
                    style={{
                      width: panelWidth('sidebar', 248, 200, 360),
                      background: 'var(--region-sidebar-bg, var(--ds-bg-panel, var(--sidebar-bg, hsl(var(--card)))))',
                    }}
                    data-testid="sidebar-panel"
                    data-region="sidebar"
                  >
                    <div className="min-h-0 flex-1 overflow-hidden">{sidebarContent}</div>
                  </aside>
                )}
                {sidebarContent && !sidebarCollapsed && (
                  <div
                    className={cn(
                      'w-1 shrink-0 cursor-col-resize self-stretch',
                      workspaceFullscreen && 'hidden',
                    )}
                    data-testid="sidebar-resize-handle"
                    onPointerDown={startPanelDrag('sidebar')}
                    title="拖动调整侧边栏宽度"
                  />
                )}

                {/* 聊天（弹性让位/全宽；顶部 40px 带由标签栏行自身占位，
                    与两侧开关按钮同排；工作区全屏时 CSS 隐藏保挂载——
                    ChatContainer 实例与滚动位置跨全屏切换保留） */}
                <div
                  className={cn(
                    'flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden',
                    workspaceFullscreen && 'hidden',
                  )}
                >
                  {chatContent}
                </div>

                {/* 工作区（让位式，无边线；顶部让出图标带；宽度可拖拽调）。
                    全屏：隐藏手柄、宽 flex-1 占满；退出恢复让位式宽度。
                    组件位置恒定——全屏切换不重挂载（重挂载=数据全量重拉） */}
                {(!workspaceCollapsed || workspaceFullscreen) && (
                  <>
                    <div
                      className={cn(
                        'w-1 shrink-0 cursor-col-resize self-stretch',
                        workspaceFullscreen && 'hidden',
                      )}
                      data-testid="workspace-resize-handle"
                      onPointerDown={startPanelDrag('workspace')}
                      title="拖动调整工作区宽度"
                    />
                    <div
                      className={cn(
                        'theme-workspace-area flex flex-col overflow-hidden',
                        workspaceFullscreen ? 'min-h-0 min-w-0 flex-1' : 'shrink-0 pt-10',
                      )}
                      style={
                        workspaceFullscreen
                          ? undefined
                          : {
                              width: panelWidth('workspace', workspaceDefaultWidth, 360, workspaceMaxWidth),
                            }
                      }
                      data-region="workspace"
                    >
                      <WorkspaceHost
                        tabs={workspaceTabs}
                        onTabChange={setActiveTab}
                        onTabClose={handleCloseTab}
                        renderTabContent={renderTabContent}
                        onFullscreen={toggleWorkspaceFullscreen}
                        isFullscreen={workspaceFullscreen}
                        visitedTabIds={visitedTabIds}
                      />
                    </div>
                  </>
                )}
              </div>
            </div>
          </section>
        )}
      </div>

      {/* ---- StatusBar 已移除（task_layout_responsive 任务 2：无常驻底栏，
           状态信息并入各区——连接小圆点在顶栏、成本在输入框、插件项在侧栏底部） ---- */}

      {/* 移动端工作区全屏覆盖层（工作区全屏态下让位于全屏工作区本体） */}
      {isMobile && mobileWorkspaceOpen && !workspaceFullscreen && (
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
