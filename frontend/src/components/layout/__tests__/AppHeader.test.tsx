/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：AppHeader 轻量化（task_layout_responsive 任务 1）
 *
 * 推演链：DSH 无顶栏理念 + 主流 AI app 顶部轻导航调研 → 设计决策「轻顶栏 =
 * ☰ + 标题 + 高频动作，44px 内」→ 功能点：
 * - 左：☰ 侧栏入口（桌面折叠/展开，移动打开抽屉）
 * - 中：`灵汐 · 当前对话标题`（无标题显示品牌）+ 连接状态小圆点
 * - 右：高频动作「工作区」按钮（桌面切换显隐，移动打开全屏视图）+ extraRight
 * - 去掉：导航（设置/监控，归侧栏）、新建对话按钮（侧栏顶部已有）、
 *   MaximizeWindow/RestoreWindow（原生窗口控制）
 *
 * 验收：顶栏只有 ☰ + 标题 + 工作区，高度 ≤ 44px；设置/监控入口在侧栏可达；
 * 桌面/移动复用同一组件。
 */

import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { queryClient as globalQueryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { createTestQueryClient } from '@/test/renderWithProviders'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { AppHeader } from '../AppHeader'

function resetStores() {
  useSessionStore.setState({ activeSessionId: null })
  globalQueryClient.clear()
  useLayoutModeStore.setState({
    connectionStatus: {
      state: 'connected',
      latencyMs: 5,
      reconnectAttempt: 0,
      lastConnectedAt: null,
      queuedMessages: 0,
    },
  })
  useUIStore.setState({ sidebarCollapsed: false, workspaceCollapsed: false })
}

function renderHeader(
  props?: { extraRight?: React.ReactNode; onOpenWorkspaceView?: () => void },
  client = createTestQueryClient(),
) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AppHeader {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppHeader 轻量化 — AI app 标准轻顶栏', () => {
  beforeEach(() => {
    resetStores()
  })

  it('三段式：左 ☰ + 中「灵汐 · 会话标题」+ 右工作区按钮', () => {
    const client = createTestQueryClient()
    client.setQueryData(queryKeys.sessions, [
      {
        id: 's1',
        title: '需求评审',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
        messageCount: 0,
      },
    ])
    useSessionStore.setState({ activeSessionId: 's1' })

    renderHeader(undefined, client)

    expect(screen.getByTestId('titlebar-toggle-sidebar')).toBeInTheDocument()
    expect(screen.getByTestId('titlebar-title')).toHaveTextContent('灵汐 · 需求评审')
    expect(screen.getByTestId('titlebar-workspace')).toBeInTheDocument()
  })

  it('无活动会话时标题退化为品牌「灵汐」', () => {
    renderHeader()
    expect(screen.getByTestId('titlebar-title')).toHaveTextContent('灵汐')
    expect(screen.getByTestId('titlebar-title')).not.toHaveTextContent('·')
  })

  it('去掉新建对话/更多菜单/最大化图标/顶栏导航（设置/监控不直接渲染在顶栏）', () => {
    renderHeader()

    expect(screen.queryByTestId('titlebar-new-session')).not.toBeInTheDocument()
    expect(screen.queryByTestId('titlebar-more-menu')).not.toBeInTheDocument()
    expect(screen.queryByTestId('titlebar-toggle-maximize')).not.toBeInTheDocument()
    expect(screen.queryByTestId('titlebar-toggle-workspace')).not.toBeInTheDocument()
    expect(screen.queryByTestId('titlebar-nav')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '设置' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '监控' })).not.toBeInTheDocument()
  })

  it('高度 ≤ 44px（CSS 变量控制）', () => {
    const { container } = renderHeader()
    const header = container.querySelector('header') as HTMLElement
    // 组件高度由 --layout-titlebar-height 变量决定，变量未定义时兜底 44px
    expect(header).toHaveStyle({ height: 'var(--layout-titlebar-height, 44px)' })
  })

  it('extraRight 挂载右侧（pending 计数等）', () => {
    renderHeader({ extraRight: <span data-testid="pending-badge">2 pending</span> })
    expect(screen.getByTestId('pending-badge')).toBeInTheDocument()
  })

  it('连接状态小圆点：connected 绿 / disconnected 红', () => {
    const { rerender } = renderHeader()
    const dot = screen.getByTestId('titlebar-connection-dot')
    expect(dot).toHaveAttribute('title', '内核已连接')

    useLayoutModeStore.setState({
      connectionStatus: {
        state: 'disconnected',
        latencyMs: null,
        reconnectAttempt: 2,
        lastConnectedAt: null,
        queuedMessages: 0,
      },
    })
    rerender(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter>
          <AppHeader />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('titlebar-connection-dot')).toHaveAttribute('title', '连接已断开')
  })

  it('桌面：点击工作区按钮切换工作区显隐', () => {
    renderHeader()

    fireEvent.click(screen.getByTestId('titlebar-workspace'))
    expect(useUIStore.getState().workspaceCollapsed).toBe(true)
    fireEvent.click(screen.getByTestId('titlebar-workspace'))
    expect(useUIStore.getState().workspaceCollapsed).toBe(false)
  })

  it('移动端：点击工作区按钮回调 onOpenWorkspaceView（打开工作区全屏视图）', () => {
    const onOpenWorkspaceView = vi.fn()
    renderHeader({ onOpenWorkspaceView, isMobile: true })

    fireEvent.click(screen.getByTestId('titlebar-workspace'))
    expect(onOpenWorkspaceView).toHaveBeenCalledTimes(1)
    // 移动端不改变桌面工作区显隐状态
    expect(useUIStore.getState().workspaceCollapsed).toBe(false)
  })

  it('☰ 切换侧栏折叠状态（桌面）', () => {
    renderHeader()
    const btn = screen.getByTestId('titlebar-toggle-sidebar')
    fireEvent.click(btn)
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    fireEvent.click(btn)
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })
})
