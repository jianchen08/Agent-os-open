/** Five Space Layout Component Implements the five-rendering-space layout: */

import React, {
  lazy,
  Suspense,
  useCallback,
  useMemo,
  useState,
  useEffect,
  useRef,
} from 'react'
import { FolderOpen, Menu, PanelRightIcon } from '@/assets/icons'
import { FullscreenComposer } from '@/components/chat/FullscreenComposer'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
// 插件查看器 webview 沙箱（声明驱动查看器路由，ADR 2026-10-24）：静态引入——
// 同一模块被消息卡/悬浮层等聊天面静态消费，动态导入无法为其单独切 chunk
// （INEFFECTIVE_DYNAMIC_IMPORT），懒包装只剩 Suspense 空壳，故按现状直连。
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
// 按需引入 antd Splitter 子模块，避免加载 antd 全量入口（26+ 组件 → 全部 icons →
// 触发 847 项 @ant-design/icons-svg/lib/asn/* 全量预构建，首屏 JS 与启动预构建时间双高）
import { isMobileViewport } from '@/hooks/useIsMobileViewport'
import { cn } from '@/lib/utils'
import { LazyFallback } from '@/router/lazyFallback'
import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS as W } from '@/services/api/endpoints.generated'
import { reportError, ErrorSeverity, ErrorType } from '@/services/errorReporting'
import {
  FILE_OPEN_CHUNK_BYTES,
  loadMoreFileContent,
  openPluginViewerTab,
} from '@/services/fileOpener'
import { resolveFileViewer } from '@/services/fileViewerRegistry'
import { safeLoadLayout } from '@/services/layout/resolver'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { openFolderWithFeedback } from '@/services/workspaceFolderOpener'
import { getFileEditorData, registerFileEditor, removeFileEditorData, updateFileEditorData } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { taskStatusToAgentTabStatus } from '@/types/taskStatus'
import { isDragReleased, suppressEmbeddedPointerEvents } from '@/utils/dragGuard'
import { isEditableEventTarget } from '@/utils/editableTarget'
import {
  BAND_BUTTON_ACTIVE_CLASS,
  BAND_BUTTON_ICON_CLASS,
  BAND_BUTTON_IDLE_CLASS,
  BAND_EDGE_PADDING_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
} from './bandButton'
import { FloatingWindowManager, renderFloatingWindowContent } from './FloatingWindowManager'
import { FullscreenOverlay } from './FullscreenOverlay'
import { useLayoutAlertNotifications } from './LayoutAlertNotifications'
import { useFileEditorAutoRefresh } from './useFileEditorAutoRefresh'
import { WorkspaceHost } from './WorkspaceHost'
// CodeEditor/FilePreview 懒加载：仅工作区文件页签首开时加载——其静态边
// （CodeEditor→LobeChatMarkdown→@lobehub/ui、prismLanguages→refractor）会把
// vendor-lobehub（≈1.1MB）+vendor-syntax（≈116KB）拖进首屏 modulepreload。
// NoLoaderPreview/HtmlPreviewWidget 零重依赖，留静态。
import { BacklinksPanel } from '../shared/markdown/BacklinksPanel'
import { NoLoaderPreview } from '../workspace/NoLoaderPreview'
// 反向链接侧栏（双链 P1）：轻组件（apiClient/fileOpener 均已在装配层依赖链内），
// 静态导入零额外 bundle 成本；仅 markdown 预览分支挂载
import type { WorkspaceTab  } from '@/types/layout'

const CodeEditor = lazy(() =>
  import('../workspace/CodeEditor').then((m) => ({ default: m.CodeEditor })),
)
const FilePreview = lazy(() =>
  import('../workspace/FilePreview').then((m) => ({ default: m.FilePreview })),
)
// 文档查看器（PDF/Word/PPT + 批注）：渲染件 pdfjs-dist/docx-preview/pptx-preview
// 体积大且非首屏必需，与 FilePreview 同款懒加载
const DocumentViewer = lazy(() =>
  import('../workspace/docViewer/DocumentViewer').then((m) => ({ default: m.DocumentViewer })),
)

/** Props for the FiveSpaceLayout component */
export interface FiveSpaceLayoutProps {
  /** Chat panel content (the existing chat interface) */
  chatContent: React.ReactNode

  /** Optional sidebar content */
  sidebarContent?: React.ReactNode

  /** Callback when layout mode toggle is requested */
  onToggleMode?: () => void

  /** 登出回调 */
  onLogout?: () => void
}

/** Five Space Layout Component Arranges the UI into five rendering spaces: */
export function FiveSpaceLayout({
  chatContent,
  sidebarContent,
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
  // 工作区全屏入 uiStore（webview 桥 workspace.fullscreen 与面板全屏钮共用
  // 同一真值；会话 UI 态不持久化）
  const workspaceFullscreen = useUIStore((s) => s.workspaceFullscreen)
  const setWorkspaceFullscreenStore = useUIStore((s) => s.setWorkspaceFullscreen)
  const toggleWorkspaceFullscreen = useCallback(
    () => setWorkspaceFullscreenStore(!useUIStore.getState().workspaceFullscreen),
    [setWorkspaceFullscreenStore],
  )
  /** 移动端工作区覆盖层是否打开 */
  // 移动端工作区覆盖层开关提升到 uiStore：workspacePanelOpener 移动端
  // 开面板时直接点亮覆盖层（否则面板开在页签层，移动端无人可见）
  const mobileWorkspaceOpen = useUIStore((s) => s.mobileWorkspaceOpen)
  const setMobileWorkspaceOpen = useUIStore((s) => s.setMobileWorkspaceOpen)
  const [viewportWidth, setViewportWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth : 1280,
  )

  // Store state
  const floatingWindows = useLayoutModeStore((s) => s.floatingWindows)
  const workspaceTabs = useLayoutModeStore((s) => s.workspaceTabs)
  /** 当前激活的工作区页签：只读看板页签（task_scene）据此收窄全屏输入浮窗作用域 */
  const activeWorkspaceTab = useMemo(
    () => workspaceTabs.find((t) => t.isActive),
    [workspaceTabs],
  )
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

  useFileEditorAutoRefresh()

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

  /** 面板拖拽调宽（左右侧边栏可拖动长短）。
   * 手柄按下后监听 pointermove 计算新宽度比例（相对主内容区），
   * 实时写入 uiStore（持久化）；抬起释放。
   * clamp 作用于各自最终比例：侧栏=光标位置比；工作区=行宽减光标
   * （右侧往左拖=增宽）。clamp 必须作用于最终比例而非原始光标比——
   * 工作区经 1-ratio 反转后，clamp 原始比会把手柄位置恒钳在 0.5 → 拖不动。
   * 收尾三道保险（根因见 utils/dragGuard：内嵌文档吞事件）：
   * ①拖拽期间屏蔽内嵌文档指针事件——拖拽途经工作区内容（iframe/webview）
   *   时事件不再被吞，手柄跟手、抬起事件也回到宿主；
   * ②移动事件 buttons===0 = 按键已松开（抬起事件被吞时的兜底）→ 立即收尾，
   *   否则松开左键后面板会继续跟随鼠标移动；
   * ③pointercancel/blur/组件卸载均收尾，监听不留尾巴。 */
  const panelDragCleanupRef = useRef<(() => void) | null>(null)
  useEffect(() => {
    return () => {
      panelDragCleanupRef.current?.()
      panelDragCleanupRef.current = null
    }
  }, [])

  const startPanelDrag = useCallback(
    (side: 'sidebar' | 'workspace') => (e: React.PointerEvent) => {
      e.preventDefault()
      const container = (e.currentTarget as HTMLElement).parentElement
      if (!container) return
      const rect = container.getBoundingClientRect()
      const restoreEmbedded = suppressEmbeddedPointerEvents()
      function onMove(ev: PointerEvent) {
        if (isDragReleased(ev)) {
          onUp()
          return
        }
        const raw = (ev.clientX - rect.left) / rect.width
        if (side === 'sidebar') {
          setSidebarRatio(Math.min(0.5, Math.max(0.12, raw)))
        } else {
          setWorkspacePanelRatio(Math.min(0.5, Math.max(0.15, 1 - raw)))
        }
      }
      function onUp() {
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
        window.removeEventListener('pointercancel', onUp)
        window.removeEventListener('blur', onUp)
        restoreEmbedded()
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        if (panelDragCleanupRef.current === onUp) panelDragCleanupRef.current = null
      }
      panelDragCleanupRef.current = onUp
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
      window.addEventListener('pointercancel', onUp)
      window.addEventListener('blur', onUp)
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- viewportWidth 是 resize 重算触发器（有意依赖）：窗口变化后默认宽度跟随，删了即行为回归
  }, [viewportWidth, sidebarRatio, workspacePanelRatio])

  /** 工作区最大宽度：主内容区 50%（拖拽上限，防挤压聊天区） */
  const workspaceMaxWidth = useMemo(() => {
    if (typeof window === 'undefined') return 600
    const container = document.querySelector('[data-region="chat"]')
    const avail = container?.getBoundingClientRect().width ?? window.innerWidth
    return Math.max(360, Math.round(avail * 0.5))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 同上：viewportWidth 驱动 resize 重算（有意依赖）
  }, [viewportWidth, sidebarRatio, workspacePanelRatio])


  // 系统异常（连接断开/审批待处理/预算超限）→ 通知中心条目（原顶栏横幅并入
  // 右侧实时通知面；动作按钮执行器在 LayoutAlertNotifications，由通知中心分发）
  useLayoutAlertNotifications()

  /** 处理任务树节点点击（对话按钮）。 通过全局管道导航服务（pipelineNavigator）实现跨会话跳转： */
  const handleTaskNodeClick = useCallback(async (node: Record<string, unknown>) => {
    const taskId = (node.id as string) ?? ''
    // 标签命名链（2026-10-16 用户拍板，全标签统一）：任务标题 → 执行 agent 名 → 泛称
    const title = (node.title as string) || (node.agent_name as string) || '子任务'
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
      // 词表归一见 types/taskStatus：未知状态落 'unknown'，不猜 running
      status: taskStatusToAgentTabStatus(node.status),
    })

    if (isMobile) {
      setMobileWorkspaceOpen(false)
      setWorkspaceFullscreenStore(false)
      setWorkspaceCollapsed(true)
    }
  }, [isMobile, setMobileWorkspaceOpen, setWorkspaceCollapsed, setWorkspaceFullscreenStore])

  /** 渲染工作区 Tab 内容 连接 Schema 渲染链路 */
  const renderTabContent = useCallback(
    (tab: WorkspaceTab) => {
      // 插件查看器页签（声明驱动，ADR 2026-10-24-file-viewer-declarative）：
      // webview 沙箱渲染插件页面，props 来自 file_viewers 声明的 render.props
      // （openFile 路由命中时建签），文件坐标经 file.open 下行事件注入
      if (tab.moduleId === '__file_viewer__') {
        return <WebviewWidget {...(tab.props ?? {})} />
      }

      // 文件编辑器/预览器标签渲染（内置查看器：自带来源，同注册表声明路由）
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
          // 部分加载（按需加载未读完）禁写：PUT 全量覆盖会把截断内容写回，
          // 丢掉未加载尾部——UI 已禁编辑，此处为数据安全第二道保险
          if (
            editorData.loadedBytes !== undefined &&
            editorData.size !== undefined &&
            editorData.loadedBytes < editorData.size
          ) {
            return false
          }
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

        /** 按需加载：加载更多/加载全部（CodeEditor 加载条入口；失败通知中心可见） */
        const handleLoadMore = async (loadAll: boolean): Promise<boolean> => {
          try {
            return await loadMoreFileContent(tab.id, { loadAll })
          } catch (error) {
            reportError(error instanceof Error ? error.message : '加载更多文件内容失败', {
              type: ErrorType.CLIENT,
              severity: ErrorSeverity.ERROR,
              component: 'FiveSpaceLayout',
              action: 'loadMoreFileContent',
              source: 'frontend',
              filePath: editorData.filePath,
            })
            return false
          }
        }

        // 「无对应的加载器」卡（未命中任何 loader 的文件类型）
        if (editorData.viewerOverride === 'none') {
          return (
            <NoLoaderPreview
              filePath={editorData.filePath}
              size={editorData.size}
              url={editorData.url}
            />
          )
        }

        // 声明驱动分发（ADR 2026-10-24-file-viewer-declarative）：widget 名来自
        // 查看器注册表（自带来源的内置声明表；内置专用组件经本装配层解析，
        // 懒加载保持）。未知 widget 名兜底 CodeEditor（坏声明不白屏）。
        const viewerWidget = resolveFileViewer(editorData.filePath).viewer.render.widget

        if (viewerWidget === 'document_viewer') {
          // 文档查看器（方案 §3.6）：pdf/docx/pptx 统一走可批注查看器。
          // 取流优先用附件 blob url，其次 workspace file-content 拼接（同 FilePreview 拼法）；
          // 批注键 = 任务坐标 + 文件路径（artifacts 批注按 artifact_id 关联，此处以
          // 文件路径作稳定键——无产物登记时同样能批注，局限见实施报告）。
          const docUrl =
            editorData.url ??
            (editorData.containerTaskId
              ? `${W.workspaces_file_content_get.replace('{container_task_id}', editorData.containerTaskId)}?path=${encodeURIComponent(editorData.filePath)}`
              : undefined)
          return (
            <Suspense fallback={LazyFallback}>
              <DocumentViewer
                filePath={editorData.filePath}
                content={editorData.content}
                url={docUrl}
                artifactId={`file:${editorData.containerTaskId}:${editorData.filePath}`}
              />
            </Suspense>
          )
        }

        // 预览类 widget 全部落 FilePreview（声明表路由产出，组件内按扩展名再分流）：
        // image_preview / pdf_preview / markdown_preview / media_preview /
        // excel_preview / binary_notice。
        // 新增预览形态只加声明表一条 + FilePreview 一个分支，装配层不改路由逻辑。
        if (viewerWidget === 'markdown_preview') {
          // md 预览挂反向链接侧栏（双链 P1，用户裁定 2026-11-21 授权最小挂载）：
          // 侧栏数据来自仓库根 backlinks.json 投影（_local 只读端点），任务工作区
          // 文件路径不在仓库索引 → 组件内自裁决"无反向链接"空态，装配层不判路径归属
          return (
            <div className="flex h-full min-w-0">
              <Suspense fallback={LazyFallback}>
                <FilePreview
                  filePath={editorData.filePath}
                  content={editorData.content}
                  size={editorData.size}
                  containerTaskId={editorData.containerTaskId}
                  url={editorData.url}
                  className="min-w-0 flex-1"
                />
              </Suspense>
              <BacklinksPanel filePath={editorData.filePath} className="w-56 shrink-0" />
            </div>
          )
        }
        if (
          viewerWidget === 'image_preview' ||
          viewerWidget === 'pdf_preview' ||
          viewerWidget === 'media_preview' ||
          viewerWidget === 'excel_preview' ||
          viewerWidget === 'binary_notice'
        ) {
          return (
            <Suspense fallback={LazyFallback}>
              <FilePreview
                filePath={editorData.filePath}
                content={editorData.content}
                size={editorData.size}
                containerTaskId={editorData.containerTaskId}
                url={editorData.url}
              />
            </Suspense>
          )
        }

        if (viewerWidget === 'html_preview') {
          return (
            <HtmlPreviewWidget
              html={editorData.content}
              filePath={editorData.filePath}
              title={editorData.fileName}
              containerTaskId={editorData.containerTaskId}
            />
          )
        }

        return (
          <Suspense fallback={LazyFallback}>
            <CodeEditor
              filePath={editorData.filePath}
              content={editorData.content}
              size={editorData.size}
              loadedBytes={editorData.loadedBytes}
              onLoadMore={handleLoadMore}
              onSave={handleSaveFile}
              tabId={tab.id}
            />
          </Suspense>
        )
      }

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
              <WidgetComponent {...(tab.props ?? {})} panel={tab.component} dataSource={tab.dataSource} />
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

            // 声明驱动路由：插件查看器命中 → webview 页签（与 openFile 同路径）
            const viewerResolved = resolveFileViewer(filePath)
            if (viewerResolved.source !== 'builtin') {
              openPluginViewerTab(filePath, viewerResolved, containerId)
              return
            }

            const tabId = `file-${containerId}-${filePath.replace(/[/\\]/g, '_')}`
            const layoutStore = useLayoutModeStore.getState()

            // 如果 Tab 已存在，直接激活
            const existingTab = layoutStore.workspaceTabs.find(t => t.id === tabId)
            if (existingTab) {
              layoutStore.setActiveTab(tabId)
              return
            }

            try {
              const resp = await apiClient.get(
                W.workspaces_file_content_get.replace('{container_task_id}', containerId),
                { params: { path: filePath, offset: 0, limit: FILE_OPEN_CHUNK_BYTES } },
              )
              if (resp.data?.success) {
                registerFileEditor(tabId, {
                  filePath,
                  fileName,
                  content: resp.data.content ?? '',
                  size: resp.data.size,
                  containerTaskId: containerId,
                  loadedBytes: resp.data.has_more === false ? undefined : resp.data.loaded,
                })
                layoutStore.addWorkspaceTab({
                  id: tabId,
                  title: fileName,
                  icon: '📄',
                  moduleId: '__file_editor__',
                  // 会话派生链继承：编辑页签跟随其所在文件树页签的归属会话
                  // （同一任务的派生链，工作区「只看当前会话」过滤同域）
                  ownerSessionId: tab.ownerSessionId,
                  isActive: true,
                  isPinned: false,
                })
              } else {
                reportError(resp.data?.message || '读取文件失败', {
                  type: ErrorType.CLIENT,
                  severity: ErrorSeverity.ERROR,
                  component: 'FiveSpaceLayout',
                  action: 'handleFileClick',
                  source: 'frontend',
                  filePath,
                })
              }
            } catch (error) {
              reportError(error instanceof Error ? error.message : '打开文件失败', {
                type: ErrorType.CLIENT,
                severity: ErrorSeverity.ERROR,
                component: 'FiveSpaceLayout',
                action: 'handleFileClick',
                source: 'frontend',
                filePath,
              })
            }
          }

          /** 处理在系统文件管理器中打开工作空间目录 */
          const handleOpenFolder = async () => {
            const containerId = tab.dataSource?.replace('workspace://', '') || ''
            if (!containerId) return
            await openFolderWithFeedback(containerId, '工作区目录')
          }

          const folderContainerId = tab.dataSource?.replace('workspace://', '') || ''

          return (
            <div className="relative h-full">
              {folderContainerId && (
                <button
                  className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md border bg-background/80 px-2 py-1 text-xs text-muted-foreground backdrop-blur-sm transition-colors hover:bg-muted hover:text-foreground"
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
                  onNodeClick={handleTaskNodeClick}
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
        // 事件目标在可编辑元素（textarea/input/contenteditable）内 → 不执行退出
        // 全屏：中文输入法候选激活/取消会对页面发 Escape keydown（用户在全屏
        // 浮窗输入框打字即触发退出→浮窗消失，R4 件②）。mobile 分支不动。
        if (isEditableEventTarget(e)) {
          if (mobileWorkspaceOpen) {
            setMobileWorkspaceOpen(false)
          }
        } else if (fullscreenActive) {
          exitFullscreen()
        } else if (workspaceFullscreen) {
          setWorkspaceFullscreenStore(false)
        } else if (mobileWorkspaceOpen) {
          setMobileWorkspaceOpen(false)
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [fullscreenActive, exitFullscreen, workspaceFullscreen, mobileWorkspaceOpen, setMobileWorkspaceOpen, setWorkspaceFullscreenStore])

  return (
    <div
      className="bg-background text-foreground flex w-screen flex-col overflow-hidden"
      // eslint-disable-next-line no-restricted-syntax -- 运行时动态值：皮肤/安全区/键盘缺口 CSS 变量 calc 拼接，Tailwind 类无法表达
      style={{
        fontFamily: 'var(--font-ui, var(--font-family))',
        // 皮肤装饰条槽位：仅"带文字的替代性条栏"（miku 标题栏/状态栏类，
        // skinRuntime 按文字内容判定）让位——整根下移/高度扣减；纯图形
        // 垂坠装饰（maid 花边）原生覆盖式零位移，变量恒 0 不占位
        // 键盘缺口补偿（imeFocusScroll 写 --ime-keyboard-gap）：iOS Safari 键盘不缩
        // 布局视口时靠该值把根高度抬上去，钉底输入区随之上移到键盘上沿；
        // Android/壳侧已缩视口的环境 gap=0，本项恒 0 零改变
        height:
          'calc(100dvh - var(--skin-chrome-top, 0px) - var(--skin-chrome-bottom, 0px) - var(--ime-keyboard-gap, 0px))',
        paddingTop: 'var(--skin-chrome-top, 0px)',
        paddingBottom: 'var(--skin-chrome-bottom, 0px)',
      }}
    >
      {/* 单树布局：工作区全屏不再切换 JSX 分支——分支切换会让
          WorkspaceHost/ChatContainer 整棵 unmount+remount（全屏切换必重载数据、
          组件状态全丢的根因）。全屏=常驻树内 CSS 隐藏侧栏/聊天 + 工作区 flex-1，
          组件恒定性保住（React 同位置同类型即复用实例）。 */}
      {/* ---- Main Content Area ---- */}
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {(isMobile || sidebarCollapsed) && !workspaceFullscreen && (
          <button
            type="button"
            onClick={() => useUIStore.getState().setSidebarCollapsed(false)}
            className="text-muted-foreground hover:bg-muted hover:text-foreground absolute left-1 z-10 flex h-7 w-7 items-center justify-center rounded-md transition-colors"
            // eslint-disable-next-line no-restricted-syntax -- 安全区顶距 calc(var(--safe-area-top))，运行时变量
            style={{ top: 'calc(0.5rem + var(--safe-area-top, 0px))' }}
            title="展开侧边栏"
            aria-label="展开侧边栏"
            data-testid="sidebar-expand-float"
          >
            <Menu className="h-4 w-4" />
          </button>
        )}
        {/* 移动端对话页右上角工作区入口（仅覆盖层关闭态渲染）：与左上汉堡同排
            对称（同一安全区顶距，安全区避让沿用 --safe-area-top 单源）。行为=
            抽屉内工作区按钮同款打开逻辑：先收抽屉再点亮覆盖层。覆盖层打开后
            本钮卸载，返回对话改由覆盖层标签行末尾槽位的 band 原生按钮承载
            （同款同排，与 + / 只看当前会话 尺寸位置一致，见覆盖层 WorkspaceHost
            的 bandTrailingSlot）——绝对定位浮钮与标签行控件坐标重叠的根因
            （2026-11-14 真机 M332BF 两轮实测）就此从构造上消除。 */}
        {isMobile && !workspaceFullscreen && !mobileWorkspaceOpen && (
          <button
            type="button"
            onClick={() => {
              useUIStore.getState().setSidebarCollapsed(true)
              setMobileWorkspaceOpen(true)
            }}
            className="text-muted-foreground hover:bg-muted hover:text-foreground absolute right-1 z-10 flex h-7 w-7 items-center justify-center rounded-md transition-colors"
            // eslint-disable-next-line no-restricted-syntax -- 安全区顶距 calc(var(--safe-area-top))，运行时变量
            style={{ top: 'calc(0.5rem + var(--safe-area-top, 0px))' }}
            title={mobileWorkspaceOpen ? '返回对话' : '工作区'}
            aria-label={mobileWorkspaceOpen ? '返回对话' : '工作区'}
            data-testid="workspace-expand-float"
          >
            <PanelRightIcon className="h-4 w-4" />
          </button>
        )}
        {/* 移动端侧边栏：抽屉，隐藏时完全不占位 */}
        {sidebarContent && isMobile && !sidebarCollapsed && (
          <div
            className="fixed inset-0 z-40"
            // eslint-disable-next-line no-restricted-syntax -- 标题栏高度变量 var(--layout-titlebar-height)，运行时变量
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
              // eslint-disable-next-line no-restricted-syntax -- 主题区域背景变量链（--region-sidebar-bg 三级回退），运行时变量
              style={{ background: 'var(--region-sidebar-bg, var(--ds-bg-panel, var(--sidebar-bg, hsl(var(--card)))))' }}
            >
              <div className="min-h-0 flex-1 overflow-hidden">{sidebarContent}</div>
              <div className="flex items-center gap-1 px-2 py-1.5">
                <button
                  type="button"
                  onClick={() => {
                    // 工作区覆盖层(z-30)低于抽屉(z-40)：不收起抽屉则覆盖层被
                    // 自家抽屉遮蔽，表现为点击无反应
                    useUIStore.getState().setSidebarCollapsed(true)
                    setMobileWorkspaceOpen(true)
                  }}
                  className="text-muted-foreground hover:bg-muted hover:text-foreground flex h-7 w-7 items-center justify-center rounded-md transition-colors"
                  title="工作区"
                  aria-label="工作区"
                  data-testid="mobile-workspace-btn"
                >
                  <PanelRightIcon className={BAND_BUTTON_ICON_CLASS} />
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
             区域间距用位置计算让位而非移动图标；面板-聊天边界有分隔线：
             侧栏侧由 Sidebar 自身 border-r 承载，工作区侧以同规格 border-l 对称。 */
          <section className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-region="chat">
            {/* 顶带：唯一拖拽容器（用户裁定：没有按钮交互的地方皆可拖动）。
                全部交互控件都是本条的 DOM 子元素并显式 app-no-drag——拖拽区域图
                只为子元素挖洞，跨组件绝对定位元素盖在带上不产生洞（点侧边栏
                反被最大化即此）。控制簇留在视口层（登录页也需要），由右侧
                同尺寸 no-drag 垫声明洞；对话标签行由 ChatContainer 经 portal
                渲入槽位（动态标签增删自动跟随）。 */}
            {/* 顶带三区（宽度跟随下方三列：面板开合/拖宽自动同步）——
                侧栏区=[侧栏开关]；聊天区=[对话标签行 portal]；
                工作区区=[工作区标签行 portal][工作区开关][控制簇槽位]。
                三区切分与内容三列严格同域：切分层不吃留白，两端留白内化为
                侧栏区/工作区区的内部内边距（区外缘仍与列边界对齐）——留白
                若留在切分层，工作区标签行会整体偏出工作区列范围。
                区内非按钮处皆为拖拽面；按钮 app-no-drag + 主题圆角（rounded-md
                = var(--radius-md)，主题变换圆角/直角自动跟随）。 */}
            <div
              className={cn(
                'app-drag-region relative flex h-10 shrink-0 items-center',
                BAND_GAP_CLASS,
              )}
              data-testid="chat-top-band"
              ref={(el) => {
                if (el) window.dispatchEvent(new Event('chat-top-band-mounted'))
              }}
            >
              <div
                className={cn(
                  'app-drag-region flex h-full shrink-0 items-center',
                  BAND_EDGE_PADDING_CLASS,
                )}
                // eslint-disable-next-line no-restricted-syntax -- 面板宽度运行时计算（panelWidth 拖拽比例），动态值
                style={{ width: sidebarContent && !sidebarCollapsed && !workspaceFullscreen ? panelWidth('sidebar', 248, 200, 360) : 44 }}
              >
                <button
                  type="button"
                  onClick={() => useUIStore.getState().setSidebarCollapsed(!sidebarCollapsed)}
                  className={cn(
                    BAND_ICON_BUTTON_CLASS,
                    !sidebarCollapsed && sidebarContent
                      ? BAND_BUTTON_ACTIVE_CLASS
                      : BAND_BUTTON_IDLE_CLASS,
                    workspaceFullscreen && 'hidden',
                  )}
                  title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
                  aria-label="侧边栏"
                  data-testid="sidebar-toggle-float"
                >
                  <Menu className={BAND_BUTTON_ICON_CLASS} />
                </button>
              </div>
              <div className={cn('app-drag-region flex h-full min-w-0 flex-1 items-center justify-center', workspaceFullscreen && 'hidden')}>
                <div
                  id="chat-top-band-tabs"
                  className="app-no-drag flex h-full min-w-0 items-center overflow-y-hidden"
                />
              </div>
              <div
                className={cn(
                  'app-drag-region flex h-full min-w-0 items-center',
                  BAND_GAP_CLASS,
                  BAND_EDGE_PADDING_CLASS,
                  workspaceFullscreen && 'flex-1',
                )}
                // eslint-disable-next-line no-restricted-syntax -- 面板宽度运行时计算（panelWidth 拖拽比例），动态值
                style={workspaceFullscreen ? undefined : { width: workspaceCollapsed ? 176 : panelWidth('workspace', workspaceDefaultWidth, 360, workspaceMaxWidth) }}
              >
                <div
                  id="chat-top-band-workspace-tabs"
                  className={cn(
                    'scrollbar-hide flex h-full min-w-0 flex-1 items-center overflow-x-auto overflow-y-hidden',
                    BAND_GAP_CLASS,
                  )}
                />
                <button
                  type="button"
                  onClick={() => setWorkspaceCollapsed(!workspaceCollapsed)}
                  className={cn(
                    BAND_ICON_BUTTON_CLASS,
                    !workspaceCollapsed
                      ? BAND_BUTTON_ACTIVE_CLASS
                      : BAND_BUTTON_IDLE_CLASS,
                    workspaceFullscreen && 'hidden',
                  )}
                  title={workspaceCollapsed ? '展开工作区' : '收起工作区'}
                  aria-label="工作区"
                  data-testid="workspace-toggle-float"
                >
                  <PanelRightIcon className="h-4 w-4" />
                </button>
                <div
                  id="chat-top-band-cluster"
                  className="app-no-drag flex h-full shrink-0 items-center"
                />
              </div>
            </div>

            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <div className="flex min-h-0 flex-1 overflow-hidden">
                {/* 侧栏（让位式，主题面板样式，无边线；顶部让出图标带；
                    背景链与 Sidebar.tsx 内层统一（--ds-bg-panel 优先），
                    图标条与侧栏内容一色；宽度可拖拽调；
                    工作区全屏时 CSS 隐藏保挂载） */}
                {sidebarContent && !sidebarCollapsed && (
                  <aside
                    className={cn(
                      'flex shrink-0 flex-col overflow-hidden',
                      workspaceFullscreen && 'hidden',
                    )}
                    // eslint-disable-next-line no-restricted-syntax -- 面板宽度运行时计算 + 主题背景变量链，动态值
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

                {/* 工作区（让位式；左边线与侧栏 border-r 同规格
                    （border-border/50），全屏独占时隐藏；顶部让出图标带；
                    宽度可拖拽调）。
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
                        workspaceFullscreen
                          ? 'min-h-0 min-w-0 flex-1'
                          : 'shrink-0 border-l border-border/50',
                      )}
                      // eslint-disable-next-line no-restricted-syntax -- 全屏/让位两态：皮肤 inset 变量 + panelWidth 运行时计算
                      style={
                        workspaceFullscreen
                          ? {
                              paddingTop: 'var(--skin-chat-inset-top, 0px)',
                              paddingBottom: 'var(--skin-chat-inset-bottom, 0px)',
                            }
                          : {
                              width: panelWidth('workspace', workspaceDefaultWidth, 360, workspaceMaxWidth),
                              // 皮肤垂坠带让位（同聊天列）：花边不压工作区顶栏/底栏
                              paddingTop: 'var(--skin-chat-inset-top, 0px)',
                              paddingBottom: 'var(--skin-chat-inset-bottom, 0px)',
                            }
                      }
                      data-region="workspace"
                      data-testid="workspace-column"
                      data-skin-inset-target="workspace"
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
                      {/* 全屏简化 composer 浮窗：仅聊天类页签需要（真实输入框全屏时被
                          CSS 隐藏，浮窗是其镜像）。现场面板是只读看板，浮窗挂在右下角
                          会压住产物文件区——故按激活页签收窄作用域，切回聊天页签照常出现。 */}
                      {activeWorkspaceTab?.component !== 'task_scene' && <FullscreenComposer />}
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

      {/* 移动端工作区全屏覆盖层：移动端工作区唯一宿主（聊天区全屏时 hidden
          让位），全屏态必须保持挂载——卸载即黑屏死路（ESC 是唯一出口而移动端
          无键盘）；inset-0 真全屏（顶带也让位，2026-11-14 用户裁定「打开工作区
          就该是全屏、不留顶带」），仅内衬安全区顶距避系统状态栏。内部不设操作
          栏——返回对话唯一出口=顶栏右上工作区浮钮（打开态 z-40 恒在覆盖层之上，
          见上方按钮渲染处注释）。覆盖层亦不传 onFullscreen：WorkspaceHost 的
          「铺满全屏」钮在真全屏下冗余，且与返回浮钮在顶栏右端重叠（2026-11-14
          真机 M332BF 实测重叠误触），移动面只留返回浮钮一个。 */}
      {isMobile && mobileWorkspaceOpen && (
        <div
          className="fixed inset-0 z-30 flex flex-col bg-background"
          data-testid="mobile-workspace-overlay"
        >
          {/* 工作区内容（safe-area-top 避系统状态栏；底部避手势条） */}
          <div
            className="safe-area-pb min-h-0 flex-1 overflow-hidden"
            // eslint-disable-next-line no-restricted-syntax -- 安全区顶距 var(--safe-area-top)，运行时变量
            style={{ paddingTop: 'var(--safe-area-top, 0px)' }}
          >
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
              visitedTabIds={visitedTabIds}
              bandTrailingSlot={
                <button
                  type="button"
                  onClick={() => setMobileWorkspaceOpen(false)}
                  className={`${BAND_ICON_BUTTON_CLASS} ${BAND_BUTTON_IDLE_CLASS}`}
                  title="返回对话"
                  aria-label="返回对话"
                  data-testid="workspace-expand-float"
                >
                  <PanelRightIcon className={BAND_BUTTON_ICON_CLASS} />
                </button>
              }
            />
          </div>
        </div>
      )}

      {/* ---- Floating Windows Container ---- */}
      <div
        className="pointer-events-none fixed inset-0"
        // eslint-disable-next-line no-restricted-syntax -- 层级来自布局配置 layoutConfig.zIndex，运行时值
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
