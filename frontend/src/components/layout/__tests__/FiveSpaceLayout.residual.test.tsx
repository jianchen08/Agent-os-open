/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * FiveSpaceLayout 残余覆盖补测：
 * - 插件查看器页签（`__file_viewer__` → webview 沙箱懒加载渲染）
 * - 任务树节点点击的移动端收尾（关闭覆盖层/退出全屏/收起工作区）
 * - 文件编辑器「部分加载禁写」守卫（onSave 拒绝且不发 PUT）
 * - 加载更多（成功推进 / 失败上报且不崩溃）
 * - 「无对应的加载器」卡（viewerOverride='none'）
 * - 文件树点击的插件查看器路由（声明驱动 → 建 webview 页签）
 * - 文件树点击读取失败信封（不建页签，保持原状）
 *
 * 行为断言口径：只断言用户可见结果（页签集合 / 渲染节点 / 是否发出写请求），
 * 不断言内部实现细节。mock 边界仅限外部依赖（HTTP 传输层、@lobehub/ui 依赖链
 * 的编辑器/预览器、预算告警网络源）。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { act } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadFileViewers } from '@/services/fileViewerRegistry'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import {
  registerFileEditor,
  removeFileEditorData,
} from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import type * as costControlMod from '@/services/api/costControl'
import type { WorkspaceTab } from '@/types/layout'

// CodeEditor 依赖链含 @lobehub/ui（vitest 不解析）：以带「保存 / 加载更多」两个
// 动作按钮的替身承接，onSave/onLoadMore 契约由组件 props 真实驱动
vi.mock('@/components/workspace/CodeEditor', async () => {
  const { useState } = await import('react')
  return {
    CodeEditor: ({
      onSave,
      onLoadMore,
      content,
      filePath,
      tabId,
    }: {
      onSave?: (c: string) => Promise<boolean>
      onLoadMore?: (all: boolean) => Promise<boolean>
      content: string
      filePath: string
      tabId: string
    }) => {
      const [saved, setSaved] = useState('')
      const [loaded, setLoaded] = useState('')
      return (
        <div data-testid="mock-code-editor" data-file={filePath} data-tab={tabId}>
          <button
            data-testid="mock-save-btn"
            onClick={async () => setSaved((await onSave?.(content)) ? 'save-ok' : 'save-fail')}
          >
            mock-save
          </button>
          <button
            data-testid="mock-load-more-btn"
            onClick={async () => setLoaded((await onLoadMore?.(false)) ? 'more' : 'done')}
          >
            mock-load-more
          </button>
          {saved && <span data-testid="mock-save-result">{saved}</span>}
          {loaded && <span data-testid="mock-load-result">{loaded}</span>}
        </div>
      )
    },
  }
})

// 插件查看器（webview 沙箱）与文件预览：仅断言「挂载了哪个查看器」，替身化重依赖
vi.mock('@/components/schema/widgets/WebviewWidget', () => ({
  WebviewWidget: (props: { widgetId?: string }) => (
    <div data-testid="mock-webview" data-widget={String(props.widgetId ?? '')} />
  ),
}))

vi.mock('@/components/workspace/FilePreview', () => ({
  FilePreview: (props: { filePath?: string }) => (
    <div data-testid="mock-file-preview" data-file={props.filePath ?? ''} />
  ),
}))

// 预算告警源（useLayoutAlerts → cost_control）：默认无告警，避免真实网络
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null as costControlMod.BudgetStatusResponse | null,
}))

// 管道导航：仅替换跳转动作（跨会话跳转的落点由 pipelineNavigator 自身测试覆盖）
vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: vi.fn().mockResolvedValue(true),
}))

// apiClient 网络层打桩（外部依赖：HTTP）。fileOpener 走命名导入、组件走默认导入——
// 两条绑定都指向同一打桩实例
const apiMock = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: apiMock, apiClient: apiMock }))

// ────────────────────────── 基建 ──────────────────────────

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, writable: true, configurable: true })
}

const chatContent = <div data-testid="chat-content">对话内容</div>
const sidebarContent = <div data-testid="sidebar-content">侧栏导航</div>

function renderLayout() {
  return render(
    <MemoryRouter>
      <FiveSpaceLayout chatContent={chatContent} sidebarContent={sidebarContent} />
    </MemoryRouter>,
  )
}

function resetStores() {
  useLayoutModeStore.setState({
    workspaceTabs: [],
    floatingWindows: [],
    fullscreenActive: false,
    fullscreenTitle: null,
    fullscreenContent: null,
    workspaceDataVersion: 0,
    connectionStatus: {
      state: 'connected',
      latencyMs: 5,
      reconnectAttempt: 0,
      lastConnectedAt: null,
      queuedMessages: 0,
    },
    pendingInteractions: [],
  })
  useUIStore.setState({
    sidebarCollapsed: false,
    workspaceCollapsed: false,
    workspaceFullscreen: false,
    mobileWorkspaceOpen: false,
    sidebarRatio: null,
    workspacePanelRatio: null,
  })
  useSessionStore.setState({ activeSessionId: null })
}

function seedTab(tab: Partial<WorkspaceTab> & { id: string }) {
  const full: WorkspaceTab = {
    title: tab.id,
    moduleId: '__dynamic__',
    isActive: true,
    isPinned: false,
    ...tab,
  }
  useLayoutModeStore.setState({ workspaceTabs: [full], visitedTabIds: [full.id] })
  return full
}

// widget 注册表测试桩（file_tree 捕获 onFileClick 供用例触发）
const registeredWidgetTypes: string[] = []
let capturedOnFileClick: ((filePath: string, fileName: string) => Promise<void>) | undefined
let capturedOnNodeClick: ((node: Record<string, unknown>) => void) | undefined

function registerFileTreeWidget() {
  widgetRegistry.register(
    'residual_file_tree',
    (props) => {
      capturedOnFileClick = props.onFileClick as typeof capturedOnFileClick
      capturedOnNodeClick = props.onNodeClick as typeof capturedOnNodeClick
      return (
        <div data-testid="residual-file-tree">
          <button
            data-testid="residual-file-btn"
            onClick={() => void props.onFileClick?.('data/sample.csv', 'sample.csv')}
          >
            file
          </button>
          <button
            data-testid="residual-node-btn"
            onClick={() => props.onNodeClick?.({ id: 'task-1', pipeline_run_id: 'run-1' })}
          >
            node
          </button>
        </div>
      )
    },
    { supportedSpaces: ['workspace'] },
  )
  registeredWidgetTypes.push('residual_file_tree')
}

const registeredEditorTabIds: string[] = []
function registerEditorData(tabId: string, data: Parameters<typeof registerFileEditor>[1]) {
  registerFileEditor(tabId, data)
  if (!registeredEditorTabIds.includes(tabId)) registeredEditorTabIds.push(tabId)
}

beforeEach(() => {
  globalThis.ResizeObserver = ResizeObserverStub as never
  setViewportWidth(1280)
  resetStores()
  apiMock.get.mockReset()
  apiMock.put.mockReset()
  apiMock.post.mockReset()
  capturedOnFileClick = undefined
  capturedOnNodeClick = undefined
  vi.mocked(navigateToPipeline).mockClear()
  loadFileViewers([])
  registerFileTreeWidget()
})

afterEach(() => {
  registeredWidgetTypes.splice(0).forEach((t) => widgetRegistry.unregister(t))
  registeredEditorTabIds.splice(0).forEach((id) => removeFileEditorData(id))
  loadFileViewers([])
  vi.restoreAllMocks()
})

// ────────────────────────── 插件查看器页签 ──────────────────────────

describe('FiveSpaceLayout 插件查看器页签', () => {
  it('__file_viewer__ 页签按 props 挂载 webview 查看器（widgetId 透传）', async () => {
    seedTab({
      id: 'viewer-1',
      title: 'sample.csv',
      moduleId: '__file_viewer__',
      component: 'webview',
      props: { widgetId: 'file-viewer-csv' },
    })

    renderLayout()

    const viewer = await screen.findByTestId('mock-webview')
    expect(viewer).toHaveAttribute('data-widget', 'file-viewer-csv')
  })

  it('无 props 的 __file_viewer__ 页签同样渲染查看器（缺省空 props）', async () => {
    seedTab({ id: 'viewer-2', title: 'unknown', moduleId: '__file_viewer__' })

    renderLayout()

    const viewer = await screen.findByTestId('mock-webview')
    expect(viewer).toHaveAttribute('data-widget', '')
  })
})

// ────────────────────────── 任务树节点点击（移动端收尾） ──────────────────────────

describe('FiveSpaceLayout 任务树节点点击 —— 移动端收尾', () => {
  it('移动端点击任务节点：跳转后关闭工作区覆盖层并收起工作区', async () => {
    setViewportWidth(375)
    seedTab({
      id: 'tree-mobile',
      title: '任务',
      component: 'residual_file_tree',
      dataSource: 'workspace://ct-mobile',
    })
    useUIStore.setState({ mobileWorkspaceOpen: true })

    renderLayout()

    await waitFor(() => expect(capturedOnNodeClick).toBeTypeOf('function'))
    act(() => {
      fireEvent.click(screen.getByTestId('residual-node-btn'))
    })

    await waitFor(() => {
      expect(navigateToPipeline).toHaveBeenCalledWith(
        'run-1',
        expect.objectContaining({ taskId: 'task-1' }),
      )
    })
    expect(useUIStore.getState().mobileWorkspaceOpen).toBe(false)
    expect(useUIStore.getState().workspaceCollapsed).toBe(true)
    expect(useUIStore.getState().workspaceFullscreen).toBe(false)
  })
})

// ────────────────────────── 文件编辑器写守卫与按需加载 ──────────────────────────

describe('FiveSpaceLayout 文件编辑器部分加载', () => {
  it('部分加载态禁止保存：不发 PUT，保存结果如实为失败', async () => {
    registerEditorData('tab-partial', {
      filePath: 'src/big.py',
      fileName: 'big.py',
      content: 'head',
      size: 1000,
      loadedBytes: 4,
      containerTaskId: 'ct-partial',
    })
    seedTab({ id: 'tab-partial', title: 'big.py', moduleId: '__file_editor__' })

    renderLayout()

    const editor = await screen.findByTestId('mock-code-editor')
    expect(editor).toHaveAttribute('data-tab', 'tab-partial')
    fireEvent.click(screen.getByTestId('mock-save-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-save-result')).toHaveTextContent('save-fail'),
    )
    expect(apiMock.put).not.toHaveBeenCalled()
  })

  it('加载更多成功：推进分页并返回「仍有更多」', async () => {
    registerEditorData('tab-more', {
      filePath: 'src/big.py',
      fileName: 'big.py',
      content: 'head',
      size: 1000,
      loadedBytes: 4,
      containerTaskId: 'ct-more',
    })
    seedTab({ id: 'tab-more', title: 'big.py', moduleId: '__file_editor__' })
    apiMock.get.mockResolvedValue({
      data: { success: true, content: '-tail', size: 1000, loaded: 9, has_more: true },
    })

    renderLayout()

    await screen.findByTestId('mock-code-editor')
    fireEvent.click(screen.getByTestId('mock-load-more-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-load-result')).toHaveTextContent('more'),
    )
    expect(apiMock.get).toHaveBeenCalledWith(
      '/ext/workspace_service/workspaces/ct-more/file-content',
      { params: { path: 'src/big.py', offset: 4, limit: 65536 } },
    )
  })

  it('加载更多传输失败：不抛出、结果为「已读完」', async () => {
    registerEditorData('tab-more-fail', {
      filePath: 'src/big.py',
      fileName: 'big.py',
      content: 'head',
      size: 1000,
      loadedBytes: 4,
      containerTaskId: 'ct-more-fail',
    })
    seedTab({ id: 'tab-more-fail', title: 'big.py', moduleId: '__file_editor__' })
    apiMock.get.mockRejectedValue(new Error('network down'))

    renderLayout()

    await screen.findByTestId('mock-code-editor')
    fireEvent.click(screen.getByTestId('mock-load-more-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-load-result')).toHaveTextContent('done'),
    )
  })
})

// ────────────────────────── 无加载器卡 ──────────────────────────

describe('FiveSpaceLayout 无对应加载器', () => {
  it('viewerOverride=none → 渲染「无对应的加载器」卡而非编辑器', async () => {
    registerEditorData('tab-noloader', {
      filePath: 'data/blob.bin',
      fileName: 'blob.bin',
      content: '',
      size: 12,
      containerTaskId: 'ct-noloader',
      viewerOverride: 'none',
    })
    seedTab({ id: 'tab-noloader', title: 'blob.bin', moduleId: '__file_editor__' })

    renderLayout()

    expect(await screen.findByText(/暂无支持 .bin 类型的加载器/)).toBeInTheDocument()
    expect(screen.queryByTestId('mock-code-editor')).not.toBeInTheDocument()
  })
})

// ────────────────────────── 文件树点击路由 ──────────────────────────

describe('FiveSpaceLayout 文件树点击', () => {
  it('命中插件查看器声明 → 建 webview 页签（不落内置编辑器）', async () => {
    loadFileViewers([
      {
        plugin_id: 'demo_plugin',
        contributes: {
          file_viewers: [
            {
              id: 'csv_viewer',
              label: 'CSV 查看器',
              extensions: ['.csv'],
              render: { widget: 'webview', props: { widgetId: 'csv-page' } },
            },
          ],
        },
      },
    ])
    seedTab({
      id: 'tree-plugin',
      title: '工作区',
      component: 'residual_file_tree',
      dataSource: 'workspace://ct-plugin',
    })

    renderLayout()

    await waitFor(() => expect(capturedOnFileClick).toBeTypeOf('function'))
    await act(async () => {
      await capturedOnFileClick?.('data/sample.csv', 'sample.csv')
    })

    const ids = useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)
    expect(ids).toContain('file-view-csv_viewer-data_sample.csv')
    const viewerTab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'file-view-csv_viewer-data_sample.csv')
    expect(viewerTab?.moduleId).toBe('__file_viewer__')
  })

  it('读取文件失败信封 → 不建页签（工作区页签保持原状）', async () => {
    seedTab({
      id: 'tree-fail',
      title: '工作区',
      component: 'residual_file_tree',
      dataSource: 'workspace://ct-fail',
    })
    apiMock.get.mockResolvedValue({ data: { success: false, message: '读取被拒绝' } })

    renderLayout()

    await waitFor(() => expect(capturedOnFileClick).toBeTypeOf('function'))
    await act(async () => {
      await capturedOnFileClick?.('docs/missing.md', 'missing.md')
    })

    const ids = useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)
    expect(ids).toEqual(['tree-fail'])
  })
})
