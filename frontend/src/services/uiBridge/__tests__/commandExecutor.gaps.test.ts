/** @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1命令闭环 @ci: frontend-test
 * commandExecutor 补充缺口测试（实现方补充件，不动签发件）：四动作参数形状
 * 防线分支（形状非法一律 rejected 且零副作用）+ 坏帧/缺 params 载荷容忍 +
 * highlight 连发替换（先到计时器被清）+ 缺省 openPage 缺声明分支（显式通知
 * 不静默）。覆盖实现改动行（diff coverage 100% 前提）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { useUIStore } from '@/stores/uiStore'
import { subscribeHighlight, startCommandExecutor } from '../commandExecutor'

const wsSubscribeMock = vi.fn()
const wsUnsubscribeMock = vi.fn()
const apiPostMock = vi.fn()
const openPluginPageMock = vi.fn()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, handler: (data: unknown) => void) => {
      wsSubscribeMock(event, handler)
    },
    unsubscribe: (event: string, handler: (data: unknown) => void) => {
      wsUnsubscribeMock(event, handler)
    },
    status: 'connected',
  },
}))

vi.mock('@/services/api/client', () => ({
  default: { post: (...args: unknown[]) => apiPostMock(...args) },
  apiClient: { post: (...args: unknown[]) => apiPostMock(...args) },
}))

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: { getPages: () => [] },
}))

vi.mock('@/services/workspacePanelOpener', () => ({
  openPluginPage: (...args: unknown[]) => openPluginPageMock(...args),
  openDeclaredPluginPage: vi.fn(),
  openWorkspacePanel: vi.fn(),
  openWorkspacePanelByPath: vi.fn(),
}))

const handlersFor = (event: string): Array<(data: unknown) => void> =>
  wsSubscribeMock.mock.calls
    .filter(([name]) => name === event)
    .map(([, handler]) => handler as (data: unknown) => void)

const emitUiCommand = (data: {
  command_id: string
  action: string
  params?: Record<string, unknown>
}): void => {
  const frame = { type: 'ui_command', data }
  for (const handler of handlersFor('ui_command')) handler(frame)
}

const executors: Array<{ stop: () => void }> = []

beforeEach(() => {
  vi.useFakeTimers()
  wsSubscribeMock.mockClear()
  wsUnsubscribeMock.mockClear()
  apiPostMock.mockClear()
  apiPostMock.mockResolvedValue({})
  openPluginPageMock.mockClear()
  useUIStore.setState({ sidebarCollapsed: false, sidebarActiveView: 'sessions' })
  useNotificationStore.setState({ notifications: [], readIds: new Set<string>() })
})

afterEach(() => {
  for (const exec of executors.splice(0)) exec.stop()
  vi.useRealTimers()
})

describe('参数形状防线（插件侧已校验，此处是直发 WS 的最后防线）', () => {
  it.each([
    ['navigate', undefined, 'navigate-无 params 键'],
    ['navigate', { target: 123 }, 'navigate-target 非字符串'],
    ['set_collapsed', {}, 'set_collapsed-缺 collapsed'],
    ['set_collapsed', { collapsed: 'yes' }, 'set_collapsed-collapsed 非布尔'],
    ['open_page', {}, 'open_page-缺 page_id'],
    ['open_page', { page_id: 7 }, 'open_page-page_id 非字符串'],
    ['highlight', {}, 'highlight-缺 item_id'],
  ])('%p params=%p → rejected 且零副作用（%s）', async (action, params) => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    const executor = startCommandExecutor({ postResult })
    executors.push(executor)

    emitUiCommand({
      command_id: 'gap-1',
      action: action as string,
      params: params as Record<string, unknown> | undefined,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(postResult).toHaveBeenCalledWith({ command_id: 'gap-1', status: 'rejected' })
    expect(useUIStore.getState().sidebarActiveView).toBe('sessions')
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
    expect(openPluginPageMock).not.toHaveBeenCalled()
  })
})

describe('坏帧容忍与 highlight 连发替换', () => {
  it('缺 command_id / 缺 data 的帧：静默忽略（零回执零副作用）', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    const executor = startCommandExecutor({ postResult })
    executors.push(executor)

    for (const frame of [{ type: 'ui_command' }, { type: 'ui_command', data: null }, { data: { action: 'navigate' } }]) {
      for (const handler of handlersFor('ui_command')) handler(frame)
    }
    await vi.advanceTimersByTimeAsync(0)

    expect(postResult).not.toHaveBeenCalled()
    expect(useUIStore.getState().sidebarActiveView).toBe('sessions')
  })

  it('连发 highlight：先到计时器被清，后到项接管高亮（listener 先 {itemId} 后 {itemId}）', async () => {
    const executor = startCommandExecutor({ postResult: vi.fn().mockResolvedValue(undefined) })
    executors.push(executor)
    const seen: Array<{ itemId: string } | null> = []
    const unsubscribe = subscribeHighlight((hl) => seen.push(hl))

    emitUiCommand({ command_id: 'hl-1', action: 'highlight', params: { item_id: 'a.first', duration_ms: 2000 } })
    emitUiCommand({ command_id: 'hl-2', action: 'highlight', params: { item_id: 'b.second', duration_ms: 2000 } })
    expect(seen).toEqual([{ itemId: 'a.first' }, { itemId: 'b.second' }])

    await vi.advanceTimersByTimeAsync(2000)
    // a.first 的计时器已被清除：只有 b.second 在 2000ms 消退一次
    expect(seen.filter((hl) => hl === null)).toHaveLength(1)
    unsubscribe()
  })

  it('缺省 postResult 回执失败不外抛（静默，插件侧 timeout 兜底）', async () => {
    apiPostMock.mockRejectedValueOnce(new Error('network down'))
    const executor = startCommandExecutor()
    executors.push(executor)

    emitUiCommand({ command_id: 'hl-3', action: 'highlight', params: { item_id: 'builtin.tasks' } })
    await vi.advanceTimersByTimeAsync(0)
    expect(apiPostMock).toHaveBeenCalledTimes(1)
  })
})

describe('缺省 openPage 缺声明分支（显式通知不静默）', () => {
  it('page 未声明：通知中心告警 + 不碰 opener + 回执照常', async () => {
    const executor = startCommandExecutor() // 缺省 postResult/openPage
    executors.push(executor)

    emitUiCommand({ command_id: 'gap-2', action: 'open_page', params: { page_id: 'ghost.page' } })
    await vi.advanceTimersByTimeAsync(0)

    expect(openPluginPageMock).not.toHaveBeenCalled()
    const notifications = useNotificationStore.getState().notifications
    expect(notifications.some((n) => n.title === '页面无法打开')).toBe(true)
    expect(apiPostMock).toHaveBeenCalledTimes(1)
    expect(apiPostMock.mock.calls[0]?.[0]).toBe('/ext/ui_bridge/command_result')
    expect(apiPostMock.mock.calls[0]?.[1]).toEqual({ command_id: 'gap-2', status: 'applied' })
  })
})
