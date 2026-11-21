/** @feature FP-0.2.待发送队列 待发送队列重同步 | @ci: frontend-test */
/**
 * usePendingInputResync.test.tsx
 *
 * 回归测试：队列条挂载 / WS 重连时对 pending 输入队列做 GET 全量对账，
 * 断线窗口内已消费条目的残留镜像被收敛清空（F10：条目永久挂"待处理"不再复现）。
 */

import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WS_LOCAL_EVENTS } from '@/constants/websocket'
import { usePendingInputResync } from '@/hooks/usePendingInputResync'
import { usePendingInputStore } from '@/stores/pendingInputStore'

// 可变 mock：fetchPendingInputs 返回内核权威全量列表
const fetchMock = vi.hoisted(() => ({ items: [] as unknown[] }))
const fetchPendingInputs = vi.hoisted(() => vi.fn(async () => fetchMock.items))

vi.mock('@/services/api/pipelines', () => ({
  fetchPendingInputs: fetchPendingInputs,
  deletePendingInput: vi.fn(),
  clearPendingInputs: vi.fn(),
}))

// globalWS：捕获订阅处理器，测试内手动触发 RECONNECTED
type WsHandler = (payload?: unknown) => void
const wsHandlers = new Map<string, WsHandler>()
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, handler: WsHandler) => {
      wsHandlers.set(event, handler)
    }),
    unsubscribe: vi.fn((event: string) => {
      wsHandlers.delete(event)
    }),
  },
}))


function item(id: string): { id: string; content: string; source: string; created_at: string } {
  return { id, content: `内容 ${id}`, source: 'user', created_at: '2026-10-23T03:00:00Z' }
}

describe('usePendingInputResync', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    wsHandlers.clear()
    usePendingInputStore.setState({ byPipeline: {} })
  })

  it('挂载即对账：本地残留条目被内核权威列表覆盖', async () => {
    usePendingInputStore.setState({
      byPipeline: { pipe: [item('stale-1'), item('stale-2')] },
    })
    fetchMock.items = [item('fresh-1')]

    renderHook(() => usePendingInputResync('pipe'))

    await waitFor(() => {
      expect(fetchPendingInputs).toHaveBeenCalledWith('pipe')
    })
    await waitFor(() => {
      const items = usePendingInputStore.getState().byPipeline['pipe']
      expect(items).toHaveLength(1)
      expect(items[0].id).toBe('fresh-1')
    })
  })

  it('内核权威列表为空（已消费）→ 本地残留收敛清空（F10 主场景）', async () => {
    usePendingInputStore.setState({
      byPipeline: { pipe: [item('stale-1')] },
    })
    fetchMock.items = []

    renderHook(() => usePendingInputResync('pipe'))

    await waitFor(() => {
      expect(usePendingInputStore.getState().byPipeline['pipe']).toEqual([])
    })
  })

  it('WS 重连触发再对账；卸载清理订阅；本地为空也拉取（无条件对账）', async () => {
    fetchMock.items = [item('fresh-1')]
    const { unmount } = renderHook(() => usePendingInputResync('pipe'))
    await waitFor(() => {
      expect(fetchPendingInputs).toHaveBeenCalledTimes(1)
    })

    // 订阅了 RECONNECTED
    expect(wsHandlers.has(WS_LOCAL_EVENTS.RECONNECTED)).toBe(true)
    await act(async () => {
      wsHandlers.get(WS_LOCAL_EVENTS.RECONNECTED)?.()
    })
    await waitFor(() => {
      expect(fetchPendingInputs).toHaveBeenCalledTimes(2)
    })

    // 卸载清理订阅
    unmount()
    expect(wsHandlers.has(WS_LOCAL_EVENTS.RECONNECTED)).toBe(false)

    // 本地镜像为空（漏接 enqueued）也必须拉权威列表：这是唯一自愈通道
    fetchPendingInputs.mockClear()
    fetchMock.items = [item('late-enqueued')]
    renderHook(() => usePendingInputResync('pipe'))
    await waitFor(() => {
      expect(fetchPendingInputs).toHaveBeenCalledTimes(1)
      expect(usePendingInputStore.getState().byPipeline['pipe']?.[0]?.id).toBe('late-enqueued')
    })
  })
})
