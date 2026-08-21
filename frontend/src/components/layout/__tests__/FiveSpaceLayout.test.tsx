/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：FiveSpaceLayout 响应式两档布局（task_layout_responsive 任务 3）
 *
 * 推演链：DSH「内容区最大化」+ 移动端兼容需求 → 决策「两档断点（768px 分界，
 * 平板=触屏桌面），一套组件」→ 功能点：
 * - 桌面/平板（≥768px）：轻顶栏 + sidebar + chat + workspace 三区并排（Splitter）
 * - 移动端（<768px）：单屏对话 + 侧滑抽屉（☰ 打开，同一 Sidebar 组件）+ 工作区全屏视图
 * - 无常驻底栏（status-bar 不存在）
 */

import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useUIStore } from '@/stores/uiStore'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { FiveSpaceLayout } from '../FiveSpaceLayout'

// CodeEditor 依赖链包含 @lobehub/ui（fluent-emoji ESM 目录导入在 vitest 不解析），
// 布局测试不关心编辑器本体，mock 掉（同 MessageContentRenderer.spacing.test 手法）
vi.mock('@/components/workspace/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="mock-code-editor" />,
}))

// budget 告警源：useLayoutAlerts → useBudgetStatus → cost_control getBudgetStatus，
// mock 网络层控制 alert_level；默认 null（无预算告警，不影响其余用例）
const mockBudget = vi.hoisted(
  () => ({ current: null as import('@/services/api/costControl').BudgetStatusResponse | null }),
)
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => mockBudget.current,
}))

// 跳转动作 spy：仅替换 openWorkspacePanelByPath（其余导出保持原实现）
vi.mock('@/services/workspacePanelOpener', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/workspacePanelOpener')>()),
  openWorkspacePanelByPath: vi.fn(() => true),
}))

// antd Splitter 依赖 ResizeObserver / matchMedia，jsdom 缺失，测试环境打桩
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
  mockBudget.current = null
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
    workspaceMaximized: false,
  })
}

describe('FiveSpaceLayout — 响应式两档（768px 分界，平板=触屏桌面）', () => {
  beforeEach(() => {
    globalThis.ResizeObserver = ResizeObserverStub as never
    setViewportWidth(1280)
    resetStores()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('桌面（≥768px）：侧栏 + 对话 + 工作区，无顶栏无底栏（布局简化 2026-08-21）', () => {
    setViewportWidth(1280)
    renderLayout()

    // 顶栏永久移除（用户裁决：全界面只留侧栏|聊天|工作区两条分割）
    expect(screen.queryByTestId('app-header')).not.toBeInTheDocument()
    // 布局 v3：左上角恒定图标组（侧栏/工作区浮出开关），无分割线
    expect(screen.getByTestId('sidebar-toggle-float')).toBeInTheDocument()
    expect(screen.getByTestId('workspace-toggle-float')).toBeInTheDocument()
    expect(screen.getByTestId('chat-content')).toBeInTheDocument()
    expect(screen.getByTestId('sidebar-content')).toBeInTheDocument()
    // 桌面形态：Sidebar 由调用方传入并以 Splitter 面板呈现
    expect(screen.getByTestId('sidebar-panel')).toBeInTheDocument()
    // 无常驻底栏（状态栏已删除，异常走 AlertBanner 浮现）
    expect(screen.queryByTestId('status-bar')).not.toBeInTheDocument()
  })

  it('768px 为分界：767 走移动形态（单屏，无 Splitter 侧栏面板）', () => {
    setViewportWidth(767)
    renderLayout()

    expect(screen.getByTestId('chat-content')).toBeInTheDocument()
    // 移动端不渲染桌面 Splitter 面板
    expect(screen.queryByTestId('sidebar-panel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('status-bar')).not.toBeInTheDocument()
  })

  it('768px 起即桌面形态（平板竖屏不单独设计，触屏桌面）', () => {
    setViewportWidth(768)
    renderLayout()
    expect(screen.getByTestId('sidebar-panel')).toBeInTheDocument()
  })

  it('移动端：☰ 打开侧滑抽屉（同一侧栏内容），再点遮罩关闭', async () => {
    setViewportWidth(375)
    renderLayout()

    // 首次进入移动端自动折叠侧栏 → 无抽屉
    await act(async () => {})
    expect(screen.queryByTestId('mobile-sidebar-drawer')).not.toBeInTheDocument()

    // ☰ 打开抽屉 → 侧栏内容出现在抽屉里
    fireEvent.click(screen.getByTestId('sidebar-expand-float'))
    expect(screen.getByTestId('mobile-sidebar-drawer')).toBeInTheDocument()
    expect(screen.getByTestId('sidebar-content')).toBeInTheDocument()

    // 点击遮罩关闭
    fireEvent.click(screen.getByTestId('mobile-sidebar-backdrop'))
    expect(screen.queryByTestId('mobile-sidebar-drawer')).not.toBeInTheDocument()
  })

  it('移动端：顶栏工作区按钮 → 工作区全屏视图（返回按钮回到对话）', () => {
    setViewportWidth(375)
    renderLayout()

    fireEvent.click(screen.getByTestId('sidebar-expand-float'))
    fireEvent.click(screen.getByTestId('mobile-workspace-btn'))
    expect(screen.getByTestId('mobile-workspace-overlay')).toBeInTheDocument()

    // 返回按钮 → 回到对话（覆盖层关闭）
    fireEvent.click(screen.getByTestId('mobile-workspace-back'))
    expect(screen.queryByTestId('mobile-workspace-overlay')).not.toBeInTheDocument()
  })

  it('移动端：工作区视图由顶栏按钮打开，Esc 退出（全屏 Esc 语义一致）', async () => {
    setViewportWidth(375)
    renderLayout()

    fireEvent.click(screen.getByTestId('sidebar-expand-float'))
    fireEvent.click(screen.getByTestId('mobile-workspace-btn'))
    expect(screen.getByTestId('mobile-workspace-overlay')).toBeInTheDocument()

    // Esc 退出全屏/工作区视图（任务 4 验收：桌面全屏 Esc 退出，移动工作区同样支持）
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' })
    })
    expect(screen.queryByTestId('mobile-workspace-overlay')).not.toBeInTheDocument()
  })

  it('窗口 resize 跨越断点：桌面 ⇄ 移动形态切换', async () => {
    renderLayout()
    expect(screen.getByTestId('sidebar-panel')).toBeInTheDocument()

    await act(async () => {
      setViewportWidth(600)
      fireEvent(window, new Event('resize'))
    })
    expect(screen.queryByTestId('sidebar-panel')).not.toBeInTheDocument()
    expect(screen.getByTestId('chat-content')).toBeInTheDocument()

    await act(async () => {
      setViewportWidth(1280)
      fireEvent(window, new Event('resize'))
    })
    // 回桌面：移动端进入时折叠的侧栏保持折叠（内容可达），☰ 可展开回三区
    expect(screen.queryByTestId('mobile-sidebar-drawer')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('sidebar-expand-float'))
    expect(screen.getByTestId('sidebar-panel')).toBeInTheDocument()
  })

  it('预算告警浮现并点击 → 打开成本看板 /cost（治理债清理 7.5.3）', async () => {
    // cost_control 真源：alert_level=warning 过阈 → AlertBanner 出现 budget 条目
    mockBudget.current = {
      scope: 'global',
      limit: 100000,
      used: 85000,
      remaining: 15000,
      usage_percent: 85,
      alert_level: 'warning',
      estimated_cost: 1.5,
    }
    renderLayout()
    await act(async () => {}) // flush getBudgetStatus → banner 浮现

    const banner = screen.getByRole('alert')
    expect(banner).toHaveTextContent('预算使用已达 85%')

    vi.mocked(openWorkspacePanelByPath).mockClear()
    await act(async () => {
      fireEvent.click(banner)
    })
    // 成本看板已声明化（cost_control contributes.pages path /cost）——直达工作区页签
    expect(openWorkspacePanelByPath).toHaveBeenCalledWith('/cost')
  })
})
