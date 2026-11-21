/** @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1命令闭环 @ci: frontend-test
 * Sidebar 高亮态消费（AC-P1-6 最小增量）：ui_command highlight 帧经真实执行器
 * 广播（globalWS mock → commandExecutor → subscribeHighlight）到渲染——目标项
 * 获得 ring 高亮，duration_ms 到点自动消退。回执外发 mock 承接。
 */
import { act, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { getSessions } from '@/services/api/session'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { startCommandExecutor } from '@/services/uiBridge/commandExecutor'
import { useUIStore } from '@/stores/uiStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type { Session } from '@/types/models'

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, getSessions: vi.fn() }
})

const PLUGIN_PAGE_ID = 'hl.plugin.page'

function seedPluginPage(): void {
  contributionRegistry.register({
    type: 'pages',
    id: PLUGIN_PAGE_ID,
    space: 'workspace',
    slot: 'activity-bar',
    title: '高亮插件页',
    widget: 'custom_widget',
    order: 2,
  })
}

const wsSubscribeMock = vi.fn()
const apiPostMock = vi.fn()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, handler: (data: unknown) => void) => {
      wsSubscribeMock(event, handler)
    },
    unsubscribe: vi.fn(),
    status: 'connected',
  },
}))

vi.mock('@/services/api/client', () => ({
  default: { post: (...args: unknown[]) => apiPostMock(...args) },
  apiClient: { post: (...args: unknown[]) => apiPostMock(...args) },
}))

const emitUiCommand = (data: {
  command_id: string
  action: string
  params: Record<string, unknown>
}): void => {
  const frame = { type: 'ui_command', data }
  for (const [name, handler] of wsSubscribeMock.mock.calls) {
    if (name === 'ui_command') (handler as (frame: unknown) => void)(frame)
  }
}

function makeSession(): Session {
  return {
    id: 'thread-hl',
    title: '高亮契约会话',
    createdAt: '2026-11-18T00:00:00Z',
    updatedAt: '2026-11-18T00:00:00Z',
    messageCount: 0,
    status: 'active',
    metadata: {},
    agentId: null,
    workspace: null,
    isolationMode: null,
    pipelineIds: ['pipe-1'],
    pinned: false,
    starred: false,
  }
}

describe('Sidebar highlight 消费（uiBridge P1 最小增量）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    wsSubscribeMock.mockClear()
    apiPostMock.mockClear()
    apiPostMock.mockResolvedValue({})
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    seedPluginPage()
    useUIStore.setState({ sidebarCollapsed: false, sidebarActiveView: 'sessions' })
  })

  afterEach(() => {
    contributionRegistry.clear()
    vi.useRealTimers()
  })

  it('highlight 帧到达：目标侧栏项获得 ring 高亮，duration_ms 到点自动消退（展开态：内置项 + 插件条目）', () => {
    const executor = startCommandExecutor()
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    // 侧栏导航区同步渲染（fake timers 下禁用 findBy* 轮询等待）
    const tasks = screen.getByTestId('sidebar-menu-tasks')
    const newSession = screen.getByTestId('new-session-button')
    const pluginEntry = screen.getByTestId(`sidebar-menu-plugin-${PLUGIN_PAGE_ID}`)
    expect(tasks.className).not.toContain('ring-1')
    expect(newSession.className).not.toContain('ring-1')

    act(() => {
      emitUiCommand({
        command_id: 'hl-sidebar-1',
        action: 'highlight',
        params: { item_id: 'builtin.tasks', duration_ms: 300 },
      })
    })
    expect(tasks.className).toContain('ring-1')
    expect(newSession.className).not.toContain('ring-1')

    act(() => {
      emitUiCommand({
        command_id: 'hl-sidebar-2',
        action: 'highlight',
        params: { item_id: PLUGIN_PAGE_ID, duration_ms: 300 },
      })
    })
    expect(pluginEntry.className).toContain('ring-1')
    expect(tasks.className).not.toContain('ring-1')

    act(() => {
      emitUiCommand({
        command_id: 'hl-sidebar-3',
        action: 'highlight',
        params: { item_id: 'builtin.new_session', duration_ms: 300 },
      })
    })
    expect(newSession.className).toContain('ring-1')
    expect(pluginEntry.className).not.toContain('ring-1')

    act(() => {
      vi.advanceTimersByTime(299)
    })
    expect(newSession.className).toContain('ring-1')
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(newSession.className).not.toContain('ring-1')

    executor.stop()
  })

  it('折叠态 rail：highlight 帧命中 rail 任务钮与插件钮', () => {
    const executor = startCommandExecutor()
    useUIStore.setState({ sidebarCollapsed: true })
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })
    const railTasks = screen.getByTestId('sidebar-rail-tasks')
    const railPlugin = screen.getByTestId(`sidebar-rail-plugin-${PLUGIN_PAGE_ID}`)
    expect(railTasks.className).not.toContain('ring-1')

    act(() => {
      emitUiCommand({
        command_id: 'hl-rail-1',
        action: 'highlight',
        params: { item_id: 'builtin.tasks', duration_ms: 300 },
      })
    })
    expect(railTasks.className).toContain('ring-1')
    expect(railPlugin.className).not.toContain('ring-1')

    act(() => {
      emitUiCommand({
        command_id: 'hl-rail-2',
        action: 'highlight',
        params: { item_id: PLUGIN_PAGE_ID, duration_ms: 300 },
      })
    })
    expect(railPlugin.className).toContain('ring-1')
    expect(railTasks.className).not.toContain('ring-1')

    executor.stop()
  })
})
