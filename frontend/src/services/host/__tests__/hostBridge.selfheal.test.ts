/** @feature FP-T12 前端适配(宿主引用桥) | @ci: frontend-test */
/**
 * hostBridge 行为锁——初始化订阅+快照、变更事件归一（异线程忽略/无载荷忽略/
 * items 非数组归空）、清除双态、重连自愈（RECONNECTED 重拉快照）、30s 低频
 * 重申订阅（失败上报 episode 去重、恢复重置）。外部依赖（apiClient/globalWS/
 * 错误上报）mock，30s 周期用 fake timers 注入可调延迟。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type WsHandler = (payload?: unknown) => void
const wsHandlers = new Map<string, Set<WsHandler>>()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, fn: WsHandler) => {
      if (!wsHandlers.has(event)) wsHandlers.set(event, new Set())
      wsHandlers.get(event)?.add(fn)
    }),
  },
  WS_LOCAL_EVENTS: { RECONNECTED: 'reconnected' },
}))

vi.mock('@/services/errorReporting', () => ({
  reportError: vi.fn(),
  ErrorType: { NETWORK: 'network' },
  ErrorSeverity: { WARNING: 'warning' },
}))

vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import apiClient from '@/services/api/client'
import { reportError } from '@/services/errorReporting'

const SERVER_EVENT = 'host_selection_changed'

/** 模块级单例（state/currentThread/hooked）：每测重置模块拿全新实例 */
async function freshBridge() {
  vi.resetModules()
  return await import('@/services/host/hostBridge')
}

const emit = (event: string, payload?: unknown) => {
  wsHandlers.get(event)?.forEach((fn) => fn(payload))
}

describe('hostBridge — 宿主引用桥', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    wsHandlers.clear()
    vi.mocked(apiClient.post).mockResolvedValue({})
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { connected: true, items: [{ label: 'obj' }], signature: 'sig-1' },
    })
    vi.mocked(apiClient.delete).mockResolvedValue({})
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('init：订阅线程 + 快照落态（订阅者收到规范化状态）', async () => {
    const { subscribeHostSelection, initHostSelection, getHostSelection } = await freshBridge()
    const seen: unknown[] = []
    subscribeHostSelection((s) => seen.push(s))
    await initHostSelection('th-1')
    expect(apiClient.post).toHaveBeenCalledWith(expect.anything(), { thread_id: 'th-1' })
    expect(getHostSelection()).toMatchObject({ connected: true, signature: 'sig-1' })
    expect(seen.at(-1)).toMatchObject({ connected: true })
  })

  it('init 快照异常（GET 拒绝/非对象形态）：保持未连接（静默，事件来了自然恢复）', async () => {
    const { initHostSelection, getHostSelection } = await freshBridge()
    vi.mocked(apiClient.get).mockRejectedValue(new Error('kernel down'))
    await initHostSelection('th-2')
    expect(getHostSelection().connected).toBe(false)
    // 非对象快照同样不落态
    vi.mocked(apiClient.get).mockResolvedValue({ data: 'garbage' })
    await initHostSelection('th-2')
    expect(getHostSelection().connected).toBe(false)
  })

  it('变更事件：同线程更新 + items 非数组归空；异线程/无载荷忽略', async () => {
    const { initHostSelection, getHostSelection } = await freshBridge()
    await initHostSelection('th-1')
    emit(SERVER_EVENT, { data: { thread_id: 'th-1', connected: true, items: 'oops', signature: 's2' } })
    expect(getHostSelection().items).toEqual([])
    emit(SERVER_EVENT, { data: { thread_id: 'th-other', connected: true, items: [], signature: 'x' } })
    expect(getHostSelection().signature).toBe('s2')
    emit(SERVER_EVENT, undefined)
    expect(getHostSelection().signature).toBe('s2')
  })

  it('清除：成功置空返回 true；失败不本地假清返回 false', async () => {
    const { initHostSelection, clearHostSelection, getHostSelection } = await freshBridge()
    await initHostSelection('th-1')
    vi.mocked(apiClient.delete).mockResolvedValue({})
    await expect(clearHostSelection()).resolves.toBe(true)
    expect(getHostSelection().items).toEqual([])
    vi.mocked(apiClient.delete).mockRejectedValue(new Error('409'))
    await expect(clearHostSelection()).resolves.toBe(false)
  })

  it('重连自愈：RECONNECTED 后重拉快照', async () => {
    const { initHostSelection } = await freshBridge()
    await initHostSelection('th-1')
    expect(apiClient.get).toHaveBeenCalledTimes(1)
    emit('reconnected')
    await vi.waitFor(() => expect(apiClient.get).toHaveBeenCalledTimes(2))
  })

  it('30s 重申订阅：失败上报一次去重，恢复后重新可报（fake timers）', async () => {
    vi.useFakeTimers()
    const { initHostSelection } = await freshBridge()
    vi.mocked(apiClient.get).mockRejectedValue(new Error('down'))
    await initHostSelection('th-1')
    vi.mocked(apiClient.post).mockRejectedValue(new Error('sub fail'))
    vi.advanceTimersByTime(30_000)
    await vi.advanceTimersByTimeAsync(0)
    expect(reportError).toHaveBeenCalledTimes(1)
    // 连续失败期：第二跳不再刷屏
    vi.advanceTimersByTime(30_000)
    await vi.advanceTimersByTimeAsync(0)
    expect(reportError).toHaveBeenCalledTimes(1)
    // 恢复成功重置 episode，再失败重新可报
    vi.mocked(apiClient.post).mockResolvedValue({})
    vi.advanceTimersByTime(30_000)
    await vi.advanceTimersByTimeAsync(0)
    vi.mocked(apiClient.post).mockRejectedValue(new Error('sub fail 2'))
    vi.advanceTimersByTime(30_000)
    await vi.advanceTimersByTimeAsync(0)
    expect(reportError).toHaveBeenCalledTimes(2)
  })
})
