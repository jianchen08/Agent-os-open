// @feature: FP-T12 侧栏导航与贡献页 | @ci: frontend-test
/**
 * Sidebar 导航/菜单/会话动作补测
 *
 * - 搜索联动：后端命中过滤会话列表；后端失败降级提示并回退全部会话
 * - 贡献页 1.5s 轮询：挂载后热注册的 activity-bar 页出现在侧栏
 * - 折叠 rail：插件入口三种形态（path/widget/容器视图）打开工作区页签、
 *   会话入口高亮切换、用户菜单（登录/切换账号/设置/监控/任务管理）
 * - 展开态用户菜单：登录/修改密码/切换账号/设置/监控/任务管理
 * - 会话列表动作：星标/置顶/复制（成功与失败上报）/编辑保存（成功、取消、
 *   失败上报）/重置消息
 * - 移动端：遮罩与关闭按钮收起侧栏、点会话/插件页后自动收起
 *
 * 网络层 mock 边界：services/api/search、services/api/session；
 * 会话列表 store 方法在 store 层 mock（内部走真网络）。
 */

import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { searchGlobal } from '@/services/api/search'
import { getSessions, getThreadSchema } from '@/services/api/session'
import { reportError } from '@/services/errorReporting'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useAuthStore } from '@/stores/authStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type * as sessionMod from '@/services/api/session'
import type * as errorReportingMod from '@/services/errorReporting'
import type { Session } from '@/types/models'

vi.mock('@/services/api/search', () => ({
  searchGlobal: vi.fn(),
}))

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<sessionMod>()
  return { ...actual, getSessions: vi.fn(), getThreadSchema: vi.fn() }
})

vi.mock('@/services/errorReporting', async (importOriginal) => {
  const actual = await importOriginal<errorReportingMod>()
  return { ...actual, reportError: vi.fn() }
})

/** Radix DropdownMenu 需要完整指针事件序列（pointerDown → pointerUp → click） */
function openDropdownMenu(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'thread-1',
    title: '测试会话',
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:00Z',
    messageCount: 3,
    status: 'active',
    metadata: {},
    agentId: 'agentos',
    workspace: null,
    isolationMode: null,
    pipelineIds: ['pipe-abc'],
    activePipelineId: 'pipe-abc',
    pinned: false,
    starred: false,
    ...overrides,
  }
}

/** 注册三类 activity-bar 插件入口 + 监控/任务管理隐藏直达页 */
function registerPluginPages(): void {
  contributionRegistry.register({
    type: 'pages', id: 'plug-path', space: 'workspace', slot: 'activity-bar',
    title: '路径页', path: '/ext/panel', pluginId: 'plugA', order: 1,
  })
  contributionRegistry.register({
    type: 'pages', id: 'plug-widget', space: 'workspace', slot: 'activity-bar',
    title: '组件页', widget: 'custom_widget', order: 2,
  })
  contributionRegistry.register({
    type: 'viewsContainers', id: 'plug-container', title: '容器页',
  })
  contributionRegistry.register({
    type: 'views', id: 'plug-container-view', title: '容器视图',
    widget: 'container_view_widget', containerId: 'plug-container',
  })
  // 菜单直达页（slot=tab，不进侧栏导航，仅供 openWorkspacePanelByPath 解析）
  contributionRegistry.register({
    type: 'pages', id: 'monitoring-page', space: 'workspace', slot: 'tab',
    title: '监控台', path: '/monitoring', widget: 'monitoring_hub',
  })
  contributionRegistry.register({
    type: 'pages', id: 'tasks-page', space: 'workspace', slot: 'tab',
    title: '任务管理', path: '/tasks', widget: 'tasks_hub',
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  contributionRegistry.clear()
  useUIStore.setState({ sidebarCollapsed: false, messageJump: null, messageSearchQuery: '' })
  useSessionStore.setState({ activeSessionId: null })
  useNotificationStore.setState({ notifications: [] })
  useAgentTabStore.setState({
    tabs: [], activeTabId: null, tabMessagesLoading: {}, unreadCounts: {},
    currentSessionId: null, pipelineTabMap: {},
  })
  useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  useAuthStore.setState({
    isAuthenticated: true,
    user: {
      id: 'user-1', username: 'alice', email: 'alice@example.com',
      role: 'admin', createdAt: '2026-01-01T00:00:00Z',
    },
    token: 'test-token',
    mustChangePassword: false,
  })
  vi.mocked(getSessions).mockResolvedValue([makeSession()])
  vi.mocked(getThreadSchema).mockResolvedValue([])
})

afterEach(() => {
  vi.restoreAllMocks()
  contributionRegistry.clear()
})

describe('搜索联动：后端命中过滤与失败降级', () => {
  it('后端返回会话命中 → 列表仅保留命中的会话', async () => {
    vi.mocked(getSessions).mockResolvedValue([
      makeSession(),
      makeSession({ id: 'thread-2', title: '另一个会话' }),
    ])
    vi.mocked(searchGlobal).mockResolvedValue({
      query: '测试', type: 'all', sessions: [{ id: 'thread-1' }], messages: [],
    })
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    fireEvent.change(screen.getByPlaceholderText('搜索会话和消息...'), {
      target: { value: '测试' },
    })

    await waitFor(() => {
      expect(screen.getByText('会话(1)')).toBeInTheDocument()
    })
    expect(screen.getByLabelText('会话: 测试会话')).toBeInTheDocument()
    expect(screen.queryByLabelText('会话: 另一个会话')).not.toBeInTheDocument()
  })

  it('后端搜索失败 → 展示降级提示且回退为全部会话', async () => {
    vi.mocked(getSessions).mockResolvedValue([
      makeSession(),
      makeSession({ id: 'thread-2', title: '另一个会话' }),
    ])
    vi.mocked(searchGlobal).mockRejectedValue(new Error('搜索服务不可用'))
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    fireEvent.change(screen.getByPlaceholderText('搜索会话和消息...'), {
      target: { value: '测试' },
    })

    await waitFor(() => {
      expect(screen.getByText('搜索暂不可用，已展示全部会话')).toBeInTheDocument()
    })
    expect(screen.getByLabelText('会话: 测试会话')).toBeInTheDocument()
    expect(screen.getByLabelText('会话: 另一个会话')).toBeInTheDocument()
  })
})

describe('贡献页 1.5s 轮询热注册', () => {
  it('挂载后注册的 activity-bar 页在下一轮轮询内出现在侧栏', async () => {
    vi.useFakeTimers()
    try {
      renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
      expect(screen.queryByTestId('sidebar-menu-plugin-hot-page')).not.toBeInTheDocument()

      contributionRegistry.register({
        type: 'pages', id: 'hot-page', space: 'workspace', slot: 'activity-bar', title: '热插页',
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500)
      })
      expect(screen.getByTestId('sidebar-menu-plugin-hot-page')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('折叠 rail：插件入口与高亮切换', () => {
  function renderRail() {
    useUIStore.setState({ sidebarCollapsed: true })
    registerPluginPages()
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
  }

  it('path 声明页：点击 rail 图标 → 经声明路径打开工作区页签', () => {
    renderRail()
    fireEvent.click(screen.getByTestId('sidebar-rail-plugin-plug-path'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({ id: 'ws-plugin-plug-path', moduleId: '__plugin_plugA__' })
  })

  it('widget 声明页：点击 rail 图标 → 打开预置 widget 页签', () => {
    renderRail()
    fireEvent.click(screen.getByTestId('sidebar-rail-plugin-plug-widget'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs[0]).toMatchObject({
      id: 'ws-plugin-plug-widget', component: 'custom_widget', moduleId: '__contrib_plug-widget__',
    })
  })

  it('容器条目：取容器下声明 widget 的视图条目打开页签', () => {
    renderRail()
    fireEvent.click(screen.getByTestId('sidebar-rail-plugin-plug-container'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs[0]).toMatchObject({
      id: 'ws-plugin-plug-container-view', component: 'container_view_widget', title: '容器视图',
    })
  })

  it('会话入口高亮跟随点击切换（插件选中后会话入口可点回）', () => {
    renderRail()
    const sessionsBtn = screen.getByTestId('sidebar-rail-sessions')
    expect(sessionsBtn.getAttribute('style')).toContain('inset')

    fireEvent.click(screen.getByTestId('sidebar-rail-plugin-plug-path'))
    expect(sessionsBtn.getAttribute('style') ?? '').not.toContain('inset')
    expect(screen.getByTestId('sidebar-rail-plugin-plug-path').getAttribute('style')).toContain('inset')

    fireEvent.click(sessionsBtn)
    expect(sessionsBtn.getAttribute('style')).toContain('inset')
  })
})

describe('任务管理常驻入口（前端自有页，非插件声明）', () => {
  it('展开态：渲染任务管理入口，点击打开 ws-panel-tasks 页签并高亮', () => {
    useUIStore.setState({ sidebarCollapsed: false })
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    fireEvent.click(screen.getByTestId('sidebar-menu-tasks'))
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-panel-tasks')
    expect(screen.getByTestId('sidebar-menu-tasks').getAttribute('style')).toContain('inset')
  })

  it('折叠 rail：点击图标打开同一页签；重复点击幂等激活不重复追加', () => {
    useUIStore.setState({ sidebarCollapsed: true })
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    fireEvent.click(screen.getByTestId('sidebar-rail-tasks'))
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-panel-tasks')

    fireEvent.click(screen.getByTestId('sidebar-rail-tasks'))
    expect(
      useLayoutModeStore.getState().workspaceTabs.filter((t) => t.id === 'ws-panel-tasks'),
    ).toHaveLength(1)
  })

  it('会话/任务高亮互斥：点任务后会话入口熄灭，点回会话恢复', () => {
    useUIStore.setState({ sidebarCollapsed: true })
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    const sessionsBtn = screen.getByTestId('sidebar-rail-sessions')
    fireEvent.click(screen.getByTestId('sidebar-rail-tasks'))
    expect(sessionsBtn.getAttribute('style') ?? '').not.toContain('inset')

    fireEvent.click(sessionsBtn)
    expect(sessionsBtn.getAttribute('style')).toContain('inset')
  })
})

describe('折叠 rail 用户菜单', () => {
  function openRailUserMenu() {
    useUIStore.setState({ sidebarCollapsed: true })
    registerPluginPages()
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    openDropdownMenu(screen.getByTestId('sidebar-rail-user'))
  }

  it('已登录：切换账号 → 打开登录弹窗', async () => {
    openRailUserMenu()
    fireEvent.click(screen.getByText('切换账号'))
    expect(await screen.findByTestId('login-modal-form')).toBeInTheDocument()
  })

  it('已登录：设置/监控/任务管理 → 分别打开对应工作区页签', async () => {
    openRailUserMenu()
    fireEvent.click(screen.getByText('设置'))
    await waitFor(() => {
      expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-panel-settings')
    })

    openDropdownMenu(screen.getByTestId('sidebar-rail-user'))
    fireEvent.click(screen.getByText('监控'))
    await waitFor(() => {
      expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-plugin-monitoring-page')
    })

    openDropdownMenu(screen.getByTestId('sidebar-rail-user'))
    fireEvent.click(screen.getByTestId('sidebar-user-menu-tasks'))
    await waitFor(() => {
      expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-plugin-tasks-page')
    })
  })

  it('未登录：登录入口 → 打开登录弹窗', async () => {
    useAuthStore.setState({ isAuthenticated: false, user: null, token: null })
    openRailUserMenu()
    fireEvent.click(screen.getByText('登录'))
    expect(await screen.findByTestId('login-modal-form')).toBeInTheDocument()
  })
})

describe('展开态用户菜单', () => {
  function openUserMenu() {
    registerPluginPages()
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
  }

  it('未登录：登录入口 → 打开登录弹窗，登录成功后弹窗自动关闭', async () => {
    useAuthStore.setState({ isAuthenticated: false, user: null, token: null })
    const loginSpy = vi
      .spyOn(useAuthStore.getState(), 'login')
      .mockImplementation(async () => {
        useAuthStore.setState({ isAuthenticated: true, token: 'new-token' })
      })
    openUserMenu()
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-login'))
    const form = await screen.findByTestId('login-modal-form')
    fireEvent.change(screen.getByTestId('login-modal-username'), { target: { value: 'bob' } })
    fireEvent.change(screen.getByTestId('login-modal-password'), { target: { value: 'secret123' } })
    fireEvent.submit(form)

    await waitFor(() => {
      expect(loginSpy).toHaveBeenCalledWith('bob', 'secret123')
    })
    // 登录态翻转 → LoginModal 自身 handleClose → 侧栏 onClose 收口
    await waitFor(() => {
      expect(screen.queryByTestId('login-modal-form')).not.toBeInTheDocument()
    })
  })

  it('已登录：修改密码 → 打开修改口令弹窗，提交成功后弹窗自动关闭', async () => {
    const changeSpy = vi
      .spyOn(useAuthStore.getState(), 'changePassword')
      .mockResolvedValue(undefined)
    openUserMenu()
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-change-password'))
    fireEvent.change(await screen.findByTestId('change-password-old-input'), {
      target: { value: 'old-pass-1' },
    })
    fireEvent.change(screen.getByTestId('change-password-new-input'), {
      target: { value: 'new-pass-123' },
    })
    fireEvent.change(screen.getByTestId('change-password-confirm-input'), {
      target: { value: 'new-pass-123' },
    })
    fireEvent.click(screen.getByTestId('change-password-submit-button'))

    await waitFor(() => {
      expect(changeSpy).toHaveBeenCalledWith('old-pass-1', 'new-pass-123')
    })
    // 表单成功回调 onSuccess → 侧栏收口 changePasswordOpen
    await waitFor(() => {
      expect(screen.queryByTestId('change-password-form')).not.toBeInTheDocument()
    })
  })

  it('已登录：切换账号 → 打开登录弹窗', async () => {
    openUserMenu()
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-switch'))
    expect(await screen.findByTestId('login-modal-form')).toBeInTheDocument()
  })

  it('已登录：设置/监控/任务管理 → 分别打开对应工作区页签', async () => {
    openUserMenu()
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-settings'))
    await waitFor(() => {
      expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-panel-settings')
    })

    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-monitoring'))
    await waitFor(() => {
      expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-plugin-monitoring-page')
    })

    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-tasks-2'))
    await waitFor(() => {
      expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-plugin-tasks-page')
    })
  })
})

describe('设置常驻入口（BUG-13：设置中枢唯一可见入口此前藏在账号下拉菜单）', () => {
  it('展开态：设置按钮不展开任何菜单即可见，点击打开设置中枢页签', () => {
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    const settingsBtn = screen.getByTestId('sidebar-settings')
    expect(settingsBtn).toBeInTheDocument()
    fireEvent.click(settingsBtn)
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({ id: 'ws-panel-settings', component: 'settings_hub' })
  })

  it('折叠态：rail 设置按钮常驻可见，重复点击幂等（激活同一页签不重复开）', () => {
    useUIStore.setState({ sidebarCollapsed: true })
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    fireEvent.click(screen.getByTestId('sidebar-rail-settings'))
    fireEvent.click(screen.getByTestId('sidebar-rail-settings'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({ id: 'ws-panel-settings', component: 'settings_hub' })
  })
})

describe('会话列表动作', () => {
  function renderWithOneSession() {
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    return waitFor(() => {
      expect(screen.getByLabelText('会话: 测试会话')).toBeInTheDocument()
    })
  }

  async function openSessionMenu() {
    await renderWithOneSession()
    openDropdownMenu(screen.getByRole('button', { name: '更多操作' }))
  }

  it('星标按钮 → toggleSessionStar（会话 id）', async () => {
    const starSpy = vi
      .spyOn(useSessionListStore.getState(), 'toggleSessionStar')
      .mockImplementation(() => {})
    await renderWithOneSession()
    fireEvent.click(screen.getByTestId('star-button'))
    expect(starSpy).toHaveBeenCalledWith('thread-1')
  })

  it('菜单置顶/星标条目 → toggleSessionPin / toggleSessionStar', async () => {
    const pinSpy = vi
      .spyOn(useSessionListStore.getState(), 'toggleSessionPin')
      .mockImplementation(() => {})
    const starSpy = vi
      .spyOn(useSessionListStore.getState(), 'toggleSessionStar')
      .mockImplementation(() => {})
    await openSessionMenu()
    fireEvent.click(screen.getByText('置顶会话'))
    expect(pinSpy).toHaveBeenCalledWith('thread-1')

    openDropdownMenu(screen.getByRole('button', { name: '更多操作' }))
    fireEvent.click(await screen.findByText('星标'))
    expect(starSpy).toHaveBeenCalledWith('thread-1')
  })

  it('菜单复制 → copySession 成功路径', async () => {
    const copySpy = vi
      .spyOn(useSessionListStore.getState(), 'copySession')
      .mockResolvedValue(undefined)
    await openSessionMenu()
    fireEvent.click(screen.getByText('复制'))
    await waitFor(() => {
      expect(copySpy).toHaveBeenCalledWith('thread-1')
    })
    // 仅约束复制动作本身无错误上报（后台轮询的 retry info 上报与本动作无关）
    expect(reportError).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ operation: 'copySession' }),
    )
  })

  it('复制失败 → reportError 上报（operation=copySession）', async () => {
    vi.spyOn(useSessionListStore.getState(), 'copySession').mockRejectedValue(
      new Error('复制服务不可用'),
    )
    await openSessionMenu()
    fireEvent.click(screen.getByText('复制'))
    await waitFor(() => {
      expect(reportError).toHaveBeenCalledWith(
        '复制服务不可用',
        expect.objectContaining({
          componentName: 'Sidebar',
          operation: 'copySession',
          sessionId: 'thread-1',
        }),
      )
    })
  })

  it('编辑 → 保存：标题走服务端改名、Agent 更新、保存后弹窗关闭', async () => {
    const renameSpy = vi
      .spyOn(useSessionListStore.getState(), 'renameSession')
      .mockResolvedValue(undefined)
    const updateAgentSpy = vi
      .spyOn(useSessionListStore.getState(), 'updateSessionAgent')
      .mockResolvedValue(undefined)
    await openSessionMenu()
    fireEvent.click(screen.getByText('编辑会话'))
    fireEvent.change(await screen.findByPlaceholderText('输入会话标题...'), {
      target: { value: '改名会话' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => {
      expect(renameSpy).toHaveBeenCalledWith('thread-1', '改名会话')
    })
    expect(updateAgentSpy).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '编辑会话' })).not.toBeInTheDocument()
    })
  })

  it('编辑 → 取消：弹窗直接关闭，不发起保存', async () => {
    const renameSpy = vi
      .spyOn(useSessionListStore.getState(), 'renameSession')
      .mockResolvedValue(undefined)
    await openSessionMenu()
    fireEvent.click(screen.getByText('编辑会话'))
    fireEvent.click(await screen.findByRole('button', { name: '取消' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '编辑会话' })).not.toBeInTheDocument()
    })
    expect(renameSpy).not.toHaveBeenCalled()
  })

  it('编辑保存失败 → reportError 上报（operation=saveSessionEdit），弹窗保留', async () => {
    vi.spyOn(useSessionListStore.getState(), 'renameSession').mockRejectedValue(
      new Error('标题保存失败'),
    )
    await openSessionMenu()
    fireEvent.click(screen.getByText('编辑会话'))
    fireEvent.click(await screen.findByRole('button', { name: '保存' }))
    await waitFor(() => {
      expect(reportError).toHaveBeenCalledWith(
        '标题保存失败',
        expect.objectContaining({
          componentName: 'Sidebar',
          operation: 'saveSessionEdit',
          sessionId: 'thread-1',
        }),
      )
    })
    expect(screen.getByRole('dialog', { name: '编辑会话' })).toBeInTheDocument()
  })

  it('重置消息 → 触发整页刷新（reload 环境限制下断言点击链路无错）', async () => {
    // jsdom 29 的 location.reload 为 [LegacyUnforgeable]（writable/configurable
    // 均 false），无法注入 spy；此处断言菜单项可点、点击不抛错且菜单收起
    // （reload 调用本身由 jsdom 静默吞掉），刷新编排归真实浏览器环境。
    await openSessionMenu()
    expect(() => fireEvent.click(screen.getByText('重置消息'))).not.toThrow()
    await waitFor(() => {
      expect(screen.queryByText('重置消息')).not.toBeInTheDocument()
    })
  })
})

describe('移动端自动收起', () => {
  it('遮罩点击 → 收起侧栏，遮罩消失', () => {
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    expect(screen.getByTestId('sidebar-overlay')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('sidebar-overlay'))
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    expect(screen.queryByTestId('sidebar-overlay')).not.toBeInTheDocument()
  })

  it('关闭按钮 → 收起侧栏', () => {
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    fireEvent.click(screen.getByTestId('close-sidebar-button'))
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
  })

  it('点击会话 → 切会话并自动收起侧栏', async () => {
    const setActiveSpy = vi
      .spyOn(useSessionListStore.getState(), 'setActiveSession')
      .mockResolvedValue(undefined)
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    fireEvent.click(await screen.findByLabelText('会话: 测试会话'))
    await waitFor(() => {
      expect(setActiveSpy).toHaveBeenCalledWith('thread-1')
    })
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
  })

  it('点击插件页 → 打开工作区页签并自动收起侧栏', () => {
    registerPluginPages()
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    fireEvent.click(screen.getByTestId('sidebar-menu-plugin-plug-path'))
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-plugin-plug-path')
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
  })

  it('点击任务管理入口 → 打开页签并自动收起侧栏', () => {
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    fireEvent.click(screen.getByTestId('sidebar-menu-tasks'))
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toContain('ws-panel-tasks')
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
  })
})
