// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * FiveSpaceLayout 覆盖缺口补充测试：
 * - 文件编辑器 3s 外部变更轮询（变更同步 / 未变跳过 / 静默失败 / 过滤条件）
 * - 工作区 Tab 内容渲染分发（file_editor 各编辑器分支 / 过期审阅 / widget 面板
 *   与降级标记 / 未知组件占位）
 * - 文件树点击开 Tab（成功 / 已存在激活 / 空 container / 传输失败）与
 *   「打开文件夹」按钮（成功 / 业务失败 / 传输失败）
 * - Tab 关闭链（file_editor 注册表清理 / 钉住保护 / 移动端关最后一个 Tab 自动收起）
 * - 桌面侧栏/工作区开关、持久化比例恢复宽度、Esc 退出全屏覆盖层
 * - AlertBanner 动作分流（连接 → 监控页；审批 → 不跳转）
 *
 * 任务树节点点击接线（2026-09-17 已修复）：handleTaskNodeClick 此前因
 * component-based 渲染路径取代 __dynamic__ 分支时接线丢失而成为死代码
 * （onNodeClick 未下发 widget props）。现 renderTabContent 已向 widget 传
 * onNodeClick={handleTaskNodeClick}，本文件的 file_tree 桩同步断言
 * 「节点点击 → navigateToPipeline 携带解析字段」与缺 run_id 早退守卫。
 * - 143/186/224/232/437/770：均为防御/兜底分支（钉住页签守卫、container null
 *   早退、querySelector 缺省宽度、移动端关最后一个 Tab）。
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import apiClient from '@/services/api/client'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: vi.fn().mockResolvedValue(undefined),
}))
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import {
  getFileEditorData,
  registerFileEditor,
  removeFileEditorData,
  subscribeFileChange,
  unsubscribeFileChange,
} from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useUIStore } from '@/stores/uiStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import type * as costControlMod from '@/services/api/costControl'
import type * as workspacePanelOpenerMod from '@/services/workspacePanelOpener'
import type { WorkspaceTab } from '@/types/layout'

// CodeEditor 依赖链含 @lobehub/ui（vitest 不解析），mock 为带保存按钮的替身：
// 点击后调用 onSave 并展示布尔结果（外部依赖契约模拟，onSave 契约为 (content) => Promise<boolean>）
vi.mock('@/components/workspace/CodeEditor', async () => {
  const { useState } = await import('react')
  return {
    CodeEditor: ({
      onSave,
      content,
      filePath,
    }: {
      onSave: (c: string) => Promise<boolean>
      content: string
      filePath: string
    }) => {
      const [result, setResult] = useState('')
      return (
        <div data-testid="mock-code-editor" data-file={filePath}>
          <button
            data-testid="mock-save-btn"
            onClick={async () => setResult((await onSave(content)) ? 'save-ok' : 'save-fail')}
          >
            mock-save
          </button>
          {result && <span data-testid="mock-save-result">{result}</span>}
        </div>
      )
    },
  }
})
vi.mock('@/components/workspace/FilePreview', () => ({
  FilePreview: (props: { filePath?: string }) => (
    <div data-testid="mock-file-preview" data-file={props.filePath ?? ''} />
  ),
}))
vi.mock('@/components/schema/widgets/HtmlPreviewWidget', () => ({
  HtmlPreviewWidget: (props: { filePath?: string }) => (
    <div data-testid="mock-html-preview" data-file={props.filePath ?? ''} />
  ),
}))

// budget 告警源（对齐既有布局测试手法）：默认无预算告警
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null as costControlMod.BudgetStatusResponse | null,
}))

// 跳转动作 spy：仅替换 openWorkspacePanelByPath
vi.mock('@/services/workspacePanelOpener', async (importOriginal) => ({
  ...(await importOriginal<workspacePanelOpenerMod>()),
  openWorkspacePanelByPath: vi.fn(() => true),
}))

// apiClient 网络层打桩（外部依赖：HTTP）
const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
}))
vi.mock('@/services/api/client', () => ({ default: apiMock }))

// ────────────────────────── 公共基建 ──────────────────────────

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
    sidebarRatio: null,
    workspacePanelRatio: null,
  })
  useNotificationStore.setState({ notifications: [] })
}

/** jsdom 无布局：给元素造非零 rect */
function mockRect(el: Element, left: number, width: number) {
  el.getBoundingClientRect = () =>
    ({
      x: left, y: 0, left, top: 0, right: left + width, bottom: 600,
      width, height: 600, toJSON: () => ({}),
    }) as DOMRect
}

/** 播种一个工作区 Tab（isActive 缺省 true） */
function seedTab(tab: Partial<WorkspaceTab> & { id: string }) {
  const full: WorkspaceTab = {
    title: tab.id,
    moduleId: '__dynamic__',
    isActive: true,
    isPinned: false,
    ...tab,
  }
  useLayoutModeStore.setState({
    workspaceTabs: [full],
    visitedTabIds: [full.id],
  })
}

// widget 注册表测试桩：记录已注册 type，测试后注销
const registeredWidgetTypes: string[] = []
function registerTestWidget(type: string, body: (props: Record<string, unknown>) => ReactNode) {
  widgetRegistry.register(type, (props) => <>{body(props)}</>, {
    supportedSpaces: ['workspace'],
  })
  registeredWidgetTypes.push(type)
}

// file_tree 测试桩捕获 onFileClick / onNodeClick，供用例触发
let capturedOnFileClick: ((filePath: string, fileName: string) => Promise<void>) | undefined
let capturedOnNodeClick: ((node: Record<string, unknown>) => void) | undefined

// fileEditorRegistry 注册过的 tabId（测试后清理，防跨用例泄漏）
const registeredEditorTabIds: string[] = []
function registerEditorData(tabId: string, data: Parameters<typeof registerFileEditor>[1]) {
  registerFileEditor(tabId, data)
  if (!registeredEditorTabIds.includes(tabId)) registeredEditorTabIds.push(tabId)
}

beforeEach(() => {
  globalThis.ResizeObserver = ResizeObserverStub as never
  setViewportWidth(1280)
  resetStores()
  vi.mocked(openWorkspacePanelByPath).mockClear()
  vi.mocked(navigateToPipeline).mockClear()
  apiMock.get.mockReset()
  apiMock.put.mockReset()
  apiMock.post.mockReset()
  capturedOnFileClick = undefined
  capturedOnNodeClick = undefined
  registerTestWidget('table', (props) => (
    <div data-testid="widget-table" data-panel={String(props.panel ?? '')}>
      table-body
    </div>
  ))
  registerTestWidget('file_tree', (props) => {
    capturedOnFileClick = props.onFileClick as typeof capturedOnFileClick
    capturedOnNodeClick = props.onNodeClick as typeof capturedOnNodeClick
    return (
      <>
        <button
          data-testid="widget-file-btn"
          onClick={() => void props.onFileClick?.('docs/readme.md', 'readme.md')}
        >
          pick-file
        </button>
        <button
          data-testid="widget-node-btn"
          onClick={() =>
            props.onNodeClick?.({
              id: 'task-9',
              title: '子任务A',
              pipeline_run_id: 'run-77',
              agent_level: 'L3',
              status: 'running',
            })
          }
        >
          pick-node
        </button>
      </>
    )
  })
})

afterEach(() => {
  registeredWidgetTypes.splice(0).forEach((t) => widgetRegistry.unregister(t))
  registeredEditorTabIds.splice(0).forEach((id) => removeFileEditorData(id))
  vi.restoreAllMocks()
})

// ────────────────────────── 文件编辑器轮询 ──────────────────────────

describe('FiveSpaceLayout 文件编辑器外部变更轮询（3s）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  function seedActiveFileTab(tabId: string, filePath: string, containerTaskId: string) {
    registerEditorData(tabId, {
      filePath,
      fileName: filePath.split('/').pop() ?? filePath,
      content: 'old',
      containerTaskId,
    })
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: tabId, title: filePath, moduleId: '__file_editor__', isActive: true, isPinned: false },
      ],
      visitedTabIds: [tabId],
    })
  }

  it('外部内容变化 → 更新注册表并广播文件变更事件', async () => {
    seedActiveFileTab('poll-a', 'notes.md', 'ct-a')
    apiMock.get.mockResolvedValue({ data: { success: true, content: 'brand new', size: 9 } })
    const changes: Array<[string, number | undefined]> = []
    const listener = (c: string, s?: number) => changes.push([c, s])
    subscribeFileChange('poll-a', listener)

    renderLayout()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(apiMock.get).toHaveBeenCalledWith(
      '/ext/workspace_service/workspaces/ct-a/file-content',
      { params: { path: 'notes.md' } },
    )
    expect(getFileEditorData('poll-a')?.content).toBe('brand new')
    expect(changes).toContainEqual(['brand new', 9])
    unsubscribeFileChange('poll-a', listener)
  })

  it('内容未变化 → 不更新不广播', async () => {
    seedActiveFileTab('poll-b', 'notes.md', 'ct-b')
    apiMock.get.mockResolvedValue({ data: { success: true, content: 'old', size: 3 } })
    const changes: unknown[] = []
    const listener = (c: string) => changes.push(c)
    subscribeFileChange('poll-b', listener)

    renderLayout()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(changes).toHaveLength(0)
    unsubscribeFileChange('poll-b', listener)
  })

  it('success:false 信封 → 不更新（防脏数据）', async () => {
    seedActiveFileTab('poll-c', 'notes.md', 'ct-c')
    apiMock.get.mockResolvedValue({ data: { success: false } })

    renderLayout()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(getFileEditorData('poll-c')?.content).toBe('old')
  })

  it('请求失败静默：不崩溃、内容保持', async () => {
    seedActiveFileTab('poll-d', 'notes.md', 'ct-d')
    apiMock.get.mockRejectedValue(new Error('network down'))

    renderLayout()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(getFileEditorData('poll-d')?.content).toBe('old')
  })

  it('无 containerTaskId 的编辑器数据跳过轮询', async () => {
    registerEditorData('poll-e', {
      filePath: 'a.txt',
      fileName: 'a.txt',
      content: 'x',
      containerTaskId: '',
    })
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'poll-e', title: 'a.txt', moduleId: '__file_editor__', isActive: true, isPinned: false },
      ],
      visitedTabIds: ['poll-e'],
    })

    renderLayout()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('非激活 / 非 file_editor Tab 不参与轮询', async () => {
    registerEditorData('poll-f', {
      filePath: 'a.py',
      fileName: 'a.py',
      content: 'x',
      containerTaskId: 'ct-f',
    })
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'poll-f', title: 'a.py', moduleId: '__file_editor__', isActive: false, isPinned: false },
        { id: 'poll-g', title: '面板', moduleId: '__panel_x', isActive: true, isPinned: false },
      ],
      visitedTabIds: ['poll-g'],
    })

    renderLayout()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(apiMock.get).not.toHaveBeenCalled()
  })
})

// ────────────────────────── Tab 内容渲染分发 ──────────────────────────

describe('FiveSpaceLayout 工作区 Tab 内容分发', () => {
  it('文件编辑器：.py 文本走 CodeEditor（带保存链）', () => {
    registerEditorData('tab-e', {
      filePath: 'src/app.py',
      fileName: 'app.py',
      content: 'file-body',
      containerTaskId: 'ct-9',
    })
    seedTab({ id: 'tab-e', title: 'app.py', moduleId: '__file_editor__' })
    renderLayout()
    expect(screen.getByTestId('mock-code-editor')).toHaveAttribute('data-file', 'src/app.py')
  })

  it('文件编辑器：图片走 FilePreview', () => {
    registerEditorData('tab-img', {
      filePath: 'assets/logo.png',
      fileName: 'logo.png',
      content: '',
      containerTaskId: 'ct-img',
    })
    seedTab({ id: 'tab-img', title: 'logo.png', moduleId: '__file_editor__' })
    renderLayout()
    expect(screen.getByTestId('mock-file-preview')).toHaveAttribute('data-file', 'assets/logo.png')
  })

  it('文件编辑器：.html 走 HtmlPreviewWidget', () => {
    registerEditorData('tab-html', {
      filePath: 'site/page.html',
      fileName: 'page.html',
      content: '<p>hi</p>',
      containerTaskId: 'ct-html',
    })
    seedTab({ id: 'tab-html', title: 'page.html', moduleId: '__file_editor__' })
    renderLayout()
    expect(screen.getByTestId('mock-html-preview')).toHaveAttribute('data-file', 'site/page.html')
  })

  it('文件编辑器：.pdf 走 FilePreview（扩展名直判）', () => {
    registerEditorData('tab-pdf', {
      filePath: 'docs/manual.pdf',
      fileName: 'manual.pdf',
      content: '',
      containerTaskId: 'ct-pdf',
    })
    seedTab({ id: 'tab-pdf', title: 'manual.pdf', moduleId: '__file_editor__' })
    renderLayout()
    expect(screen.getByTestId('mock-file-preview')).toHaveAttribute('data-file', 'docs/manual.pdf')
  })

  it('文件编辑器数据过期（注册表无数据）→ 过期占位', () => {
    seedTab({ id: 'tab-gone', title: 'gone.py', moduleId: '__file_editor__' })
    renderLayout()
    expect(screen.getByText('文件数据已过期')).toBeInTheDocument()
  })

  it('旧 __file_review__ Tab → 提示过期请关闭', () => {
    seedTab({ id: 'tab-rev', title: 'review', moduleId: '__file_review__' })
    renderLayout()
    expect(screen.getByText('此审阅 Tab 已过期，请关闭')).toBeInTheDocument()
  })

  it('__panel_ 内置面板：命中注册 widget 直接渲染，无降级标记', () => {
    seedTab({ id: 'tab-panel', title: '面板', moduleId: '__panel_demo', component: 'table' })
    renderLayout()
    expect(screen.getByTestId('widget-table')).toBeInTheDocument()
    expect(document.querySelector('[data-fallback]')).toBeNull()
  })

  it('__builtin_ 面板：组件未注册走降级映射，命中后带 data-fallback 标记', () => {
    seedTab({ id: 'tab-builtin', title: '看板', moduleId: '__builtin_k', component: 'kanban' })
    renderLayout()
    // kanban 未注册 → findFallback 落 table 组件（注册表中唯一可用降级候选）
    expect(screen.getByTestId('widget-table')).toBeInTheDocument()
    expect(document.querySelector('[data-fallback="kanban"]')).not.toBeNull()
  })

  it('未知组件：模块内容不可用占位（占位正文重复展示 Tab 标题）', () => {
    seedTab({ id: 'tab-unk', title: '神秘模块', moduleId: '__dynamic__', component: 'nope_widget' })
    renderLayout()
    expect(screen.getAllByText('神秘模块').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('模块内容不可用')).toBeInTheDocument()
  })
})

describe('FiveSpaceLayout 任务树节点点击 → 管道导航接线', () => {
  function seedTreeTabForNodes() {
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'tab-tree', title: '任务树', moduleId: '__dynamic__', component: 'file_tree', dataSource: 'workspace://ct-7', isActive: true, isPinned: false },
      ],
      visitedTabIds: ['tab-tree'],
    })
  }

  it('widget 收到 onNodeClick；节点点击 → navigateToPipeline 携带解析字段', async () => {
    seedTreeTabForNodes()
    renderLayout()
    expect(capturedOnNodeClick).toBeTypeOf('function')

    fireEvent.click(screen.getByTestId('widget-node-btn'))
    await act(async () => {})

    expect(navigateToPipeline).toHaveBeenCalledWith(
      'run-77',
      expect.objectContaining({ taskId: 'task-9', agentName: '子任务A', agentLevel: 3 }),
    )
  })

  it('缺 pipeline_run_id 的节点 → 不导航（早退守卫）', async () => {
    seedTreeTabForNodes()
    renderLayout()
    capturedOnNodeClick?.({ id: 'task-9', title: '子任务A' })
    await act(async () => {})
    expect(navigateToPipeline).not.toHaveBeenCalled()
  })
})

// ────────────────────────── 保存链（经 mock CodeEditor 触发 onSave） ──────────────────────────

describe('FiveSpaceLayout 文件保存链', () => {
  function seedEditableFile(containerTaskId: string) {
    registerEditorData('tab-save', {
      filePath: 'src/app.py',
      fileName: 'app.py',
      content: 'file-body',
      containerTaskId,
    })
    seedTab({ id: 'tab-save', title: 'app.py', moduleId: '__file_editor__' })
  }

  it('保存成功：PUT 文件内容端点（path 参数）且结果如实展示', async () => {
    seedEditableFile('ct-9')
    apiMock.put.mockResolvedValueOnce({ data: { success: true } })
    renderLayout()
    fireEvent.click(screen.getByTestId('mock-save-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-save-result')).toHaveTextContent('save-ok'),
    )
    expect(apiMock.put).toHaveBeenCalledWith(
      '/ext/workspace_service/workspaces/ct-9/file-content',
      { content: 'file-body' },
      { params: { path: 'src/app.py' } },
    )
  })

  it('保存业务失败（success:false）→ 展示失败', async () => {
    seedEditableFile('ct-9')
    apiMock.put.mockResolvedValueOnce({ data: { success: false } })
    renderLayout()
    fireEvent.click(screen.getByTestId('mock-save-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-save-result')).toHaveTextContent('save-fail'),
    )
  })

  it('保存传输失败（请求抛错）→ 展示失败不崩溃', async () => {
    seedEditableFile('ct-9')
    apiMock.put.mockRejectedValueOnce(new Error('io'))
    renderLayout()
    fireEvent.click(screen.getByTestId('mock-save-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-save-result')).toHaveTextContent('save-fail'),
    )
  })

  it('无 containerTaskId → 不发请求直接失败', async () => {
    seedEditableFile('')
    renderLayout()
    fireEvent.click(screen.getByTestId('mock-save-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('mock-save-result')).toHaveTextContent('save-fail'),
    )
    expect(apiMock.put).not.toHaveBeenCalled()
  })
})

// ────────────────────────── 文件树点击与打开文件夹 ──────────────────────────

describe('FiveSpaceLayout 文件树 Tab（file_tree widget 联动）', () => {
  function seedTreeTab(dataSource: string | undefined) {
    seedTab({
      id: 'tab-tree',
      title: '文件树',
      moduleId: '__dynamic__',
      component: 'file_tree',
      ...(dataSource !== undefined ? { dataSource } : {}),
    })
  }

  it('有 dataSource 时渲染「打开文件夹」按钮', () => {
    seedTreeTab('workspace://ct-7')
    renderLayout()
    expect(screen.getByTitle('在系统文件管理器中打开')).toBeInTheDocument()
  })

  it('无 dataSource 时不渲染「打开文件夹」按钮，文件点击不发请求', async () => {
    seedTreeTab(undefined)
    renderLayout()
    expect(screen.queryByTitle('在系统文件管理器中打开')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('widget-file-btn'))
    await act(async () => {})
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('文件点击：拉取内容 → 注册 file_editor 数据 → 新开编辑 Tab', async () => {
    seedTreeTab('workspace://ct-7')
    apiMock.get.mockResolvedValueOnce({ data: { success: true, content: 'md body', size: 7 } })
    renderLayout()

    fireEvent.click(screen.getByTestId('widget-file-btn'))

    const newTabId = 'file-ct-7-docs_readme.md'
    await waitFor(() =>
      expect(
        useLayoutModeStore.getState().workspaceTabs.some((t) => t.id === newTabId),
      ).toBe(true),
    )
    expect(apiMock.get).toHaveBeenCalledWith(
      '/ext/workspace_service/workspaces/ct-7/file-content',
      { params: { path: 'docs/readme.md' } },
    )
    const data = getFileEditorData(newTabId)
    expect(data).toMatchObject({
      filePath: 'docs/readme.md',
      fileName: 'readme.md',
      content: 'md body',
      size: 7,
      containerTaskId: 'ct-7',
    })
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) => t.id === newTabId)
    expect(tab).toMatchObject({ title: 'readme.md', moduleId: '__file_editor__' })
    registeredEditorTabIds.push(newTabId)
  })

  it('Tab 已存在 → 仅激活已有 Tab，不重复请求', async () => {
    const existingId = 'file-ct-7-docs_readme.md'
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: existingId, title: 'readme.md', moduleId: '__file_editor__', isActive: false, isPinned: false },
        { id: 'tab-tree', title: '文件树', moduleId: '__dynamic__', component: 'file_tree', dataSource: 'workspace://ct-7', isActive: true, isPinned: false },
      ],
      visitedTabIds: ['tab-tree'],
    })
    renderLayout()

    fireEvent.click(screen.getByTestId('widget-file-btn'))
    await act(async () => {})

    expect(apiMock.get).not.toHaveBeenCalled()
    expect(
      useLayoutModeStore.getState().workspaceTabs.find((t) => t.id === existingId)?.isActive,
    ).toBe(true)
  })

  it('dataSource 缺容器 id（workspace:// 裸前缀）→ 点击不发请求', async () => {
    seedTreeTab('workspace://')
    renderLayout()
    fireEvent.click(screen.getByTestId('widget-file-btn'))
    await act(async () => {})
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('文件内容请求失败 → 静默（不新开 Tab）', async () => {
    seedTreeTab('workspace://ct-7')
    apiMock.get.mockRejectedValueOnce(new Error('boom'))
    renderLayout()

    fireEvent.click(screen.getByTestId('widget-file-btn'))
    await act(async () => {})

    expect(
      useLayoutModeStore.getState().workspaceTabs.some((t) => t.id === 'file-ct-7-docs_readme.md'),
    ).toBe(false)
  })

  it('打开文件夹：调 workspaces open 端点（项目登记通道）', async () => {
    seedTreeTab('workspace://ct-7')
    apiMock.post.mockResolvedValueOnce({ data: { success: true } })
    renderLayout()
    fireEvent.click(screen.getByTitle('在系统文件管理器中打开'))

    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith('/ext/workspace_service/workspaces/ct-7/open'),
    )
  })

  it('打开文件夹业务失败 → 失败通知携带后端 message', async () => {
    seedTreeTab('workspace://ct-7')
    apiMock.post.mockResolvedValueOnce({ data: { success: false, message: '目录缺失' } })
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    renderLayout()
    fireEvent.click(screen.getByTitle('在系统文件管理器中打开'))

    await waitFor(() =>
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({ title: '打开文件夹失败', message: '目录缺失' }),
      ),
    )
    addNotification.mockRestore()
  })

  it('打开文件夹传输失败 → 静默降级', async () => {
    seedTreeTab('workspace://ct-7')
    apiMock.post.mockRejectedValueOnce(new Error('down'))
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    renderLayout()
    fireEvent.click(screen.getByTitle('在系统文件管理器中打开'))

    await act(async () => {})
    expect(addNotification).not.toHaveBeenCalled()
    addNotification.mockRestore()
  })
})

// ────────────────────────── Tab 关闭与面板开关 ──────────────────────────

describe('FiveSpaceLayout Tab 关闭与面板开关', () => {
  it('关闭 file_editor Tab：清理注册表数据并移除 Tab', () => {
    registerEditorData('tab-c', {
      filePath: 'a.py',
      fileName: 'a.py',
      content: 'x',
      containerTaskId: 'ct-c',
    })
    seedTab({ id: 'tab-c', title: 'a.py', moduleId: '__file_editor__' })
    renderLayout()

    fireEvent.click(screen.getByTestId('workspace-tab-close-tab-c'))

    expect(getFileEditorData('tab-c')).toBeUndefined()
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
  })

  it('钉住页签不可关闭：不渲染关闭按钮，右键菜单关闭项禁用', () => {
    seedTab({ id: 'tab-pin', title: '主页', moduleId: '__builtin_home', isPinned: true })
    renderLayout()

    expect(screen.queryByTestId('workspace-tab-close-tab-pin')).not.toBeInTheDocument()
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-tab-pin'))
    expect(screen.getByTestId('workspace-tab-menu-close')).toBeDisabled()
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })

  it('桌面侧栏开关：点击折叠 / 再点展开', () => {
    renderLayout()
    fireEvent.click(screen.getByTestId('sidebar-toggle-float'))
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    fireEvent.click(screen.getByTestId('sidebar-toggle-float'))
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })

  it('桌面工作区开关：点击折叠（工作区面板卸载）', () => {
    renderLayout()
    expect(document.querySelector('[data-testid="workspace-column"]')).not.toBeNull()

    fireEvent.click(screen.getByTestId('workspace-toggle-float'))

    expect(useUIStore.getState().workspaceCollapsed).toBe(true)
    // 折叠后工作区列卸载；顶带内的工作区标签槽位仍在（槽位≠列）
    expect(document.querySelector('[data-testid="workspace-column"]')).toBeNull()
  })

  it('工作区-聊天边界分隔线与侧栏同规格：非全屏带 border-l，全屏独占时隐藏', () => {
    renderLayout()
    const column = document.querySelector('[data-testid="workspace-column"]') as HTMLElement
    expect(column.className).toContain('border-l')
    expect(column.className).toContain('border-border/50')

    fireEvent.click(screen.getByTestId('workspace-toggle-fullscreen'))
    const fullColumn = document.querySelector('[data-testid="workspace-column"]') as HTMLElement
    expect(fullColumn.className).not.toContain('border-l')
  })

  it('持久化比例恢复：面板宽度 = 比例 × 主内容区宽（clamp 生效）', () => {
    useUIStore.setState({ sidebarRatio: 0.3, workspacePanelRatio: 0.4 })
    renderLayout()
    // 首帧容器尚未挂载（回退 1200 宽基准）；挂载后造 rect 并触发 resize 重算
    mockRect(document.querySelector('[data-region="chat"]') as Element, 0, 1000)
    act(() => {
      setViewportWidth(1281)
      fireEvent(window, new Event('resize'))
    })

    // 侧栏 1000×0.3=300（clamp 200~360 内）；工作区 1000×0.4=400（clamp 360~500 内）
    expect(screen.getByTestId('sidebar-panel').style.width).toBe('300px')
    const panels = document.querySelectorAll('[data-region="workspace"]')
    expect((panels[panels.length - 1] as HTMLElement).style.width).toBe('400px')
  })

  it('Esc 优先退出全屏覆盖层（fullscreenActive）', () => {
    useLayoutModeStore.setState({
      fullscreenActive: true,
      fullscreenTitle: '覆盖层标题',
      fullscreenContent: <div data-testid="fullscreen-body">覆盖层内容</div>,
    })
    renderLayout()
    expect(screen.getByTestId('fullscreen-body')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(useLayoutModeStore.getState().fullscreenActive).toBe(false)
  })

  it('移动端工作区覆盖层：关闭最后一个 Tab 自动收起返回对话', async () => {
    setViewportWidth(375)
    seedTab({
      id: 'mob-1',
      title: '移动文件树',
      moduleId: '__dynamic__',
      component: 'file_tree',
      dataSource: 'workspace://c9',
    })
    renderLayout()
    await act(async () => {})
    fireEvent.click(screen.getByTestId('sidebar-expand-float'))
    fireEvent.click(screen.getByTestId('mobile-workspace-btn'))
    expect(screen.getByTestId('mobile-workspace-overlay')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('workspace-tab-close-mob-1'))
    await act(async () => {})

    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
    expect(screen.queryByTestId('mobile-workspace-overlay')).not.toBeInTheDocument()
  })
})

// ────────────────────────── AlertBanner 动作分流 ──────────────────────────

describe('FiveSpaceLayout 异常提示条动作分流', () => {
  it('连接断开告警点击 → 打开监控面板', async () => {
    useLayoutModeStore.setState({
      connectionStatus: {
        state: 'disconnected',
        latencyMs: null,
        reconnectAttempt: 1,
        lastConnectedAt: null,
        queuedMessages: 0,
      },
    })
    renderLayout()

    fireEvent.click(await screen.findByRole('alert'))

    expect(openWorkspacePanelByPath).toHaveBeenCalledWith('/monitoring')
  })

  it('审批待处理告警点击不跳转（审批弹窗全局可见）', async () => {
    useLayoutModeStore.setState({
      pendingInteractions: [{ id: 'i1' }] as never,
    })
    renderLayout()

    fireEvent.click(await screen.findByRole('alert'))

    expect(openWorkspacePanelByPath).not.toHaveBeenCalled()
  })
})
