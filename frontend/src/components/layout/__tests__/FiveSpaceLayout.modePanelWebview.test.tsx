/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * FiveSpaceLayout × 模式面板 webview 渲染链（GUI 旅程实证 P0 回归锁）
 *
 * 契约：contributes.pages 声明 widget:'webview' 的模式面板页（六包同构，此处以
 * mode_godot 清单原样声明为样本）经工作区页签打开后，必须走 WebviewWidget
 * iframe（srcDoc 沙箱 + bootstrap 桥），不得退化为 html_preview / 占位 /
 * 「模块内容不可用」（声明↔注册断链的静默替换均已根除，见 WidgetRegistry.findFallback）。
 *
 * 链路：真实 ContributionRegistry → openWorkspacePanelByPath → layoutModeStore 页签
 * → FiveSpaceLayout renderTabContent → widgetRegistry.get('webview') → WebviewWidget。
 * 仅 HTTP 外部依赖打桩（apiClient），渲染链全真。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type * as costControlMod from '@/services/api/costControl'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useUIStore } from '@/stores/uiStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import { ResizeObserverStub, setViewportWidth } from './helpers/fiveSpaceTestUtils'
import { initializeWidgets } from '@/services/schema/registerWidgets'

// CodeEditor 依赖链在 vitest 不解析（同既有布局测试手法），布局测试不关心编辑器本体
vi.mock('@/components/workspace/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="mock-code-editor" />,
}))

// budget 告警源：默认无预算告警
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null as costControlMod.BudgetStatusResponse | null,
}))

// apiClient 网络层打桩（外部依赖：HTTP）。面板 HTML 带 head（六包实测形态），
// 经 P0 修复的 wrapHtml 包装后应同时含 CSP 与 bootstrap 桥。
const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
}))
vi.mock('@/services/api/client', () => ({
  default: apiMock,
  apiClient: apiMock,
  isNotFoundError: () => false,
}))

// mode_godot/plugin.json contributes.pages[0] 原样声明（webview 页 + detachable）
const GODOT_PAGE: PageDeclaration = {
  type: 'pages',
  id: 'godot_dev',
  title: 'Godot 开发',
  icon: '🎮',
  space: 'workspace',
  slot: 'tab',
  path: '/p/godot_dev',
  widget: 'webview',
  props: { pluginId: 'mode_godot', htmlPath: '/page/godot-panel', widgetId: 'godot_dev' },
  order: 30,
  mode: 'godot',
  pluginId: 'mode_godot',
  detachable: { popout: true, childWindow: true, defaultSize: { w: 400, h: 680 } },
} as unknown as PageDeclaration

describe('FiveSpaceLayout — 模式面板页签走 webview iframe（godot 样本）', () => {
  beforeEach(() => {
    globalThis.ResizeObserver = ResizeObserverStub as never
    setViewportWidth(1280)
    // 生产链路（main.tsx 启动时）注册内建 widget——webview 页分发依赖此注册表
    initializeWidgets()
    contributionRegistry.clear()
    apiMock.get.mockReset()
    apiMock.get.mockResolvedValue({
      data: '<html><head><title>Godot 开发</title></head><body><div id="edText">检测编辑器…</div></body></html>',
    })
    useLayoutModeStore.setState({
      workspaceTabs: [],
      floatingWindows: [],
      fullscreenActive: false,
      fullscreenTitle: null,
      fullscreenContent: null,
      workspaceDataVersion: 0,
      visitedTabIds: [],
    })
    useUIStore.setState({ sidebarCollapsed: false, workspaceCollapsed: false })
  })

  it('path 直达 godot 面板 → 页签内容为 webview iframe，桥注入完整，无任何回退标记', async () => {
    contributionRegistry.register(GODOT_PAGE)
    expect(openWorkspacePanelByPath('/p/godot_dev')).toBe(true)

    render(
      <MemoryRouter>
        <FiveSpaceLayout
          chatContent={<div data-testid="chat-content" />}
          sidebarContent={<div data-testid="sidebar-content" />}
        />
      </MemoryRouter>,
    )

    // 页签打开，归属声明页
    expect(screen.getByTestId('workspace-tab-ws-plugin-godot_dev')).toBeInTheDocument()

    // webview 路径：拉插件 HTML 端点（非文件路径、非占位）
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(apiMock.get).toHaveBeenCalledTimes(1)
    expect(String(apiMock.get.mock.calls[0][0])).toBe('/ext/mode_godot/page/godot-panel')

    // iframe srcDoc 沙箱 + bootstrap 桥完整（head 形态 HTML 不丢 window.agentos）
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const srcDoc = iframe.getAttribute('srcdoc') ?? ''
    expect(srcDoc).toContain('Content-Security-Policy')
    expect(srcDoc).toContain('window.agentos')
    expect(srcDoc).toContain('__ready')
    expect(iframe.getAttribute('sandbox') ?? '').not.toContain('allow-same-origin')

    // 无回退痕迹：html_preview 降级 / 组件占位 / 桥断错误态均不得出现
    expect(screen.queryByText('模块内容不可用')).not.toBeInTheDocument()
    expect(screen.queryByText(/组件 webview 未注册/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Webview 加载失败/)).not.toBeInTheDocument()
    expect(document.querySelector('[data-fallback]')).toBeNull()
  })
})
