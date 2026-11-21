/** @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1命令闭环 | @vision V2 全能闭环 | @ci frontend-test */
/**
 * uiBridge 前端命令执行器验收测试（AC-P1-4/5，TDD Red——API 契约由本文件定义）
 *
 * 需求真源（只读，冲突回报调度方）：
 * - docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md §2.4（命令闭环契约逐字）、§三 AC-P1-4/5
 * - docs/decisions/2026-11-18-sidebar-ui-object-model.md
 *
 * 被测契约 = ../commandExecutor.ts（实现方按本契约交付，模块当前不存在 →
 * 整文件 import 失败属预期 Red）：
 *
 *   export interface UiCommandData {
 *     command_id: string
 *     region?: string
 *     action: string
 *     params: Record<string, unknown>
 *   }
 *   export interface CommandReceiptBody {
 *     command_id: string
 *     status: 'applied' | 'rejected'
 *     reason?: string
 *   }
 *   export interface CommandExecutorDeps {
 *     // 回执外发注入点（唯一 HTTP mock 点）；缺省 = 真实 POST /ext/ui_bridge/command_result
 *     //（走 frontend/src/services/api 既有客户端，Authorization 内积惯例）
 *     postResult?: (body: CommandReceiptBody) => Promise<void>
 *     // open_page 注入点；缺省 = workspacePanelOpener 既有路径
 *     //（先例 = main.tsx registerGlobalOpenPanelCallback：contributionRegistry 按
 *     //  page id 解析声明 → openPluginPage(page)）
 *     openPage?: (pageId: string) => unknown
 *   }
 *   export function startCommandExecutor(deps?: CommandExecutorDeps): { stop(): void }
 *   // highlight 高亮态订阅面（Sidebar.tsx 高亮最小增量的对接点；uiStore 不新增字段）：
 *   export type HighlightListener = (highlight: { itemId: string } | null) => void
 *   export function subscribeHighlight(listener: HighlightListener): () => void
 *
 * 行为锚（冻结件 §2.4 逐字）：
 * - 订阅 ui_command（帧载荷在 .data，仓内 WS 事件载荷惯例 = handler 收全帧）；
 * - navigate→切 sidebarActiveView；set_collapsed→uiStore.setSidebarCollapsed；
 *   open_page→workspacePanelOpener 既有路径；highlight→高亮态 ≤5s 自动消退
 *   （duration_ms 缺省 2000，≤5000 封顶）；
 * - 未知 action 回执 rejected；每动作（含 rejected）POST 回执携 command_id；
 * - 双订阅者（多端）各执行各回执（AC-P1-5）。
 *
 * mock 仅限注入点：WS（globalWS 模块 mock：捕获订阅 + 测试内发帧）、HTTP
 * （postResult 注入 / apiClient 模块 mock）、openPage（注入 / opener 模块 mock）。
 * store 行为断言走真实 zustand store（断行为不断实现）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/services/query/queryClient'
import { useUIStore } from '@/stores/uiStore'
import { subscribeHighlight, startCommandExecutor } from '../commandExecutor'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

// ── WS 外部依赖 mock：捕获订阅、测试内定向发帧（仓内 handler 收全帧惯例）──

const wsSubscribeMock = vi.fn()
const wsUnsubscribeMock = vi.fn()

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

const handlersFor = (event: string): Array<(data: unknown) => void> =>
  wsSubscribeMock.mock.calls
    .filter(([name]) => name === event)
    .map(([, handler]) => handler as (data: unknown) => void)

const emitUiCommand = (data: {
  command_id: string
  region?: string
  action: string
  params: Record<string, unknown>
}): void => {
  const frame = { type: 'ui_command', data }
  for (const handler of handlersFor('ui_command')) handler(frame)
}

// ── HTTP 外部依赖 mock：默认回执闭包 = 既有 api 客户端 ──────────────────

const apiPostMock = vi.fn()

vi.mock('@/services/api/client', () => ({
  default: { post: (...args: unknown[]) => apiPostMock(...args) },
  apiClient: { post: (...args: unknown[]) => apiPostMock(...args) },
}))

// ── open_page 既有路径 mock：声明解析 + opener（main.tsx 先例同构）───────

const openPluginPageMock = vi.fn()
const declaredPage: PageDeclaration = {
  type: 'pages',
  id: 'rp.writer',
  title: '写作台',
  space: 'chat',
  slot: 'activity-bar',
  order: 10,
}

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    getPages: () => [declaredPage],
    getPluginPages: (_pluginId: string) => [declaredPage],
  },
}))

vi.mock('@/services/workspacePanelOpener', () => ({
  openPluginPage: (...args: unknown[]) => openPluginPageMock(...args),
  openDeclaredPluginPage: vi.fn(),
  openWorkspacePanel: vi.fn(),
  openWorkspacePanelByPath: vi.fn(),
}))

// ── 用例 ──────────────────────────────────────────────────────────────

const COMMAND_IDS = ['cmd-navigate-1', 'cmd-collapse-2', 'cmd-open-3', 'cmd-hl-4', 'cmd-unknown-5']

const makeExecutor = (deps?: Parameters<typeof startCommandExecutor>[0]) => {
  const exec = startCommandExecutor(deps)
  executors.push(exec)
  return exec
}

const executors: Array<{ stop: () => void }> = []

const postCallsOf = (post: ReturnType<typeof vi.fn>) =>
  post.mock.calls.map((call) => call[0] as Record<string, unknown>)

beforeEach(() => {
  vi.useFakeTimers()
  wsSubscribeMock.mockClear()
  wsUnsubscribeMock.mockClear()
  apiPostMock.mockClear()
  apiPostMock.mockResolvedValue({})
  openPluginPageMock.mockClear()
  useUIStore.setState({ sidebarCollapsed: false, sidebarActiveView: 'sessions' })
})

afterEach(() => {
  for (const exec of executors.splice(0)) exec.stop()
  queryClient.clear()
  vi.useRealTimers()
})

describe('订阅面（§2.4 订阅 ui_command）', () => {
  it('startCommandExecutor 即订阅 ui_command 事件', () => {
    makeExecutor({ postResult: vi.fn().mockResolvedValue(undefined) })
    expect(handlersFor('ui_command').length).toBeGreaterThanOrEqual(1)
  })
})

describe('四动作映射（AC-P1-4）', () => {
  it('navigate→切 sidebarActiveView（tasks/sessions 两组区分度输入），每动作回执 applied 携 command_id', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    makeExecutor({ postResult })

    emitUiCommand({ command_id: COMMAND_IDS[0], region: 'sidebar', action: 'navigate', params: { target: 'tasks' } })
    expect(useUIStore.getState().sidebarActiveView).toBe('tasks')

    emitUiCommand({
      command_id: `${COMMAND_IDS[0]}-b`,
      region: 'sidebar',
      action: 'navigate',
      params: { target: 'sessions' },
    })
    expect(useUIStore.getState().sidebarActiveView).toBe('sessions')

    await vi.advanceTimersByTimeAsync(0)
    const bodies = postCallsOf(postResult)
    expect(bodies).toHaveLength(2)
    expect(bodies[0]).toEqual({ command_id: COMMAND_IDS[0], status: 'applied' })
    expect(bodies[1]).toEqual({ command_id: `${COMMAND_IDS[0]}-b`, status: 'applied' })
  })

  it('set_collapsed→uiStore.setSidebarCollapsed（true/false 双向），回执 applied', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    makeExecutor({ postResult })

    emitUiCommand({ command_id: COMMAND_IDS[1], action: 'set_collapsed', params: { collapsed: true } })
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    emitUiCommand({ command_id: `${COMMAND_IDS[1]}-b`, action: 'set_collapsed', params: { collapsed: false } })
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)

    await vi.advanceTimersByTimeAsync(0)
    const bodies = postCallsOf(postResult)
    expect(bodies).toEqual([
      { command_id: COMMAND_IDS[1], status: 'applied' },
      { command_id: `${COMMAND_IDS[1]}-b`, status: 'applied' },
    ])
  })

  it('open_page→注入 openPage 携 page_id（§2.4 既有路径注入点），回执 applied', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    const openPage = vi.fn()
    makeExecutor({ postResult, openPage })

    emitUiCommand({ command_id: COMMAND_IDS[2], action: 'open_page', params: { page_id: 'rp.writer' } })
    expect(openPage).toHaveBeenCalledTimes(1)
    expect(openPage).toHaveBeenCalledWith('rp.writer')

    await vi.advanceTimersByTimeAsync(0)
    expect(postCallsOf(postResult)).toEqual([{ command_id: COMMAND_IDS[2], status: 'applied' }])
  })

  it('open_page 缺省接线 = workspacePanelOpener 既有路径：声明解析后 openPluginPage 携声明 id', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    makeExecutor({ postResult }) // 不注入 openPage → 走缺省既有路径

    emitUiCommand({ command_id: COMMAND_IDS[2], action: 'open_page', params: { page_id: 'rp.writer' } })

    expect(openPluginPageMock).toHaveBeenCalledTimes(1)
    expect(openPluginPageMock).toHaveBeenCalledWith(expect.objectContaining({ id: 'rp.writer' }))
    await vi.advanceTimersByTimeAsync(0)
    expect(postCallsOf(postResult)).toEqual([{ command_id: COMMAND_IDS[2], status: 'applied' }])
  })
})

describe('highlight 高亮态（≤5s 自动消退）', () => {
  it('高亮即通知 {itemId}，duration_ms 到点消退通知 null（300ms 短时窗）', async () => {
    makeExecutor({ postResult: vi.fn().mockResolvedValue(undefined) })
    const seen: Array<{ itemId: string } | null> = []
    const unsubscribe = subscribeHighlight((hl) => seen.push(hl))

    emitUiCommand({
      command_id: COMMAND_IDS[3],
      action: 'highlight',
      params: { item_id: 'builtin.tasks', duration_ms: 300 },
    })
    expect(seen).toEqual([{ itemId: 'builtin.tasks' }])

    await vi.advanceTimersByTimeAsync(299)
    expect(seen).toEqual([{ itemId: 'builtin.tasks' }], '300ms 窗内不提前消退')
    await vi.advanceTimersByTimeAsync(1)
    expect(seen).toEqual([{ itemId: 'builtin.tasks' }, null], 'duration_ms 到点消退')
    unsubscribe()
  })

  it('duration_ms 缺省 2000ms；duration_ms=8000 按 ≤5s 封顶消退（5000 非 8000）', async () => {
    makeExecutor({ postResult: vi.fn().mockResolvedValue(undefined) })
    const seen: Array<{ itemId: string } | null> = []
    const unsubscribe = subscribeHighlight((hl) => seen.push(hl))

    emitUiCommand({ command_id: COMMAND_IDS[3], action: 'highlight', params: { item_id: 'builtin.tasks' } })
    await vi.advanceTimersByTimeAsync(1999)
    expect(seen).toEqual([{ itemId: 'builtin.tasks' }], '缺省 2000ms 未到不消退')
    await vi.advanceTimersByTimeAsync(1)
    expect(seen[seen.length - 1]).toBeNull()
    unsubscribe()

    // 封顶：>5000 的 duration_ms 按 5000 消退
    const seenCapped: Array<{ itemId: string } | null> = []
    const unsubscribeCapped = subscribeHighlight((hl) => seenCapped.push(hl))
    emitUiCommand({
      command_id: `${COMMAND_IDS[3]}-cap`,
      action: 'highlight',
      params: { item_id: 'builtin.new_session', duration_ms: 8000 },
    })
    await vi.advanceTimersByTimeAsync(4999)
    expect(seenCapped).toEqual([{ itemId: 'builtin.new_session' }], '5s 封顶窗口内不消退')
    await vi.advanceTimersByTimeAsync(1)
    expect(seenCapped[seenCapped.length - 1]).toBeNull()
    unsubscribeCapped()
  })

  it('highlight 动作同样回执 applied 携 command_id', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    makeExecutor({ postResult })
    emitUiCommand({
      command_id: COMMAND_IDS[3],
      action: 'highlight',
      params: { item_id: 'builtin.tasks', duration_ms: 100 },
    })
    await vi.advanceTimersByTimeAsync(200)
    expect(postCallsOf(postResult)).toEqual([{ command_id: COMMAND_IDS[3], status: 'applied' }])
  })
})

describe('未知 action 回执 rejected（§2.4 逐字）', () => {
  it.each(['scroll', ''])('未知 action=%p 回执 rejected 携 command_id，且不触碰 store/高亮', async (action) => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    makeExecutor({ postResult })
    const seen: Array<{ itemId: string } | null> = []
    const unsubscribe = subscribeHighlight((hl) => seen.push(hl))

    emitUiCommand({ command_id: COMMAND_IDS[4], action, params: {} })
    await vi.advanceTimersByTimeAsync(0)

    expect(postCallsOf(postResult)).toEqual([{ command_id: COMMAND_IDS[4], status: 'rejected' }])
    expect(seen).toEqual([])
    expect(useUIStore.getState().sidebarActiveView).toBe('sessions')
    unsubscribe()
  })

  it('缺省 postResult 闭包 = 既有 api 客户端 POST /ext/ui_bridge/command_result', async () => {
    makeExecutor() // 不注入 postResult → 真实外发面（apiClient mock 承接）
    emitUiCommand({ command_id: COMMAND_IDS[4], action: 'unknown_thing', params: {} })
    await vi.advanceTimersByTimeAsync(0)

    expect(apiPostMock).toHaveBeenCalledTimes(1)
    const [url, body] = apiPostMock.mock.calls[0] as [string, Record<string, unknown>]
    expect(url).toBe('/ext/ui_bridge/command_result')
    expect(body).toEqual({ command_id: COMMAND_IDS[4], status: 'rejected' })
  })
})

describe('多端并存（AC-P1-5）', () => {
  it('双订阅者各自执行、各报回执（同一帧 → 两份 applied 回执、同 store 语义各走一遍）', async () => {
    const postA = vi.fn().mockResolvedValue(undefined)
    const postB = vi.fn().mockResolvedValue(undefined)
    makeExecutor({ postResult: postA })
    makeExecutor({ postResult: postB })

    emitUiCommand({ command_id: COMMAND_IDS[1], action: 'set_collapsed', params: { collapsed: true } })
    await vi.advanceTimersByTimeAsync(0)

    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    expect(postCallsOf(postA)).toEqual([{ command_id: COMMAND_IDS[1], status: 'applied' }])
    expect(postCallsOf(postB)).toEqual([{ command_id: COMMAND_IDS[1], status: 'applied' }])
  })
})

describe('stop() 拆除（无残留订阅）', () => {
  it('stop 后退订 ui_command，后续帧零回执零副作用', async () => {
    const postResult = vi.fn().mockResolvedValue(undefined)
    const exec = makeExecutor({ postResult })
    const subscribed = handlersFor('ui_command')
    expect(subscribed.length).toBeGreaterThanOrEqual(1)

    exec.stop()
    expect(wsUnsubscribeMock).toHaveBeenCalledWith('ui_command', subscribed[0])

    emitUiCommand({ command_id: COMMAND_IDS[1], action: 'set_collapsed', params: { collapsed: true } })
    await vi.advanceTimersByTimeAsync(0)
    expect(postResult).not.toHaveBeenCalled()
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })
})
