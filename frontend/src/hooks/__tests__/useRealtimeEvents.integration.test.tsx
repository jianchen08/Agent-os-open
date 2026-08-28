// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * useRealtimeEvents WS 订阅行为测试
 *
 * @feature FP-T12 前端适配 | @ci frontend-test
 */

import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

// ---------------------------------------------------------------------------
//  Mock: GlobalWebSocket（真实订阅面：useRealtimeEvents 经 globalWS 订阅）
// ---------------------------------------------------------------------------
const listeners: Record<string, Set<(...args: any[]) => void>> = {}

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, cb: (...args: any[]) => void) => {
      if (!listeners[event]) listeners[event] = new Set()
      listeners[event].add(cb)
    }),
    unsubscribe: vi.fn((event: string, cb: (...args: any[]) => void) => {
      listeners[event]?.delete(cb)
    }),
    send: vi.fn(),
    connect: vi.fn(),
    status: 'connected',
  },
}))

/** 触发 WebSocket 事件 */
function emitEvent(event: string, data: Record<string, unknown>) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of cbs) cb(data)
}

describe('useRealtimeEvents 订阅行为', () => {
  beforeEach(() => {
    for (const key of Object.keys(listeners)) delete listeners[key]
    useLayoutModeStore.setState({
      activeExecutions: [],
      pendingInteractions: [],
    })
  })

  it('sub_agent_* 死事件不再写入 activeExecutions（无发射源，防止死接线复活）', () => {
    renderHook(() => useRealtimeEvents())

    act(() => {
      emitEvent('sub_agent_created', {
        agentId: 'agent-rt-1',
        agentName: 'Realtime Agent',
        agentLevel: 3,
        parentAgentId: 'orchestrator',
      })
    })

    // 后端（kernel + 插件 event-bus）从不发射 sub_agent_created；
    // 若此断言失败说明有人重新挂上了无人投递的订阅
    expect(useLayoutModeStore.getState().activeExecutions).toHaveLength(0)
  })

  it('task_deleted 事件经 globalWS 真实订阅链路更新缓存', async () => {
    // 批次 4 query 化：tasks 数据在 query cache（queryKeys.longTermTasks），
    // 经全局 queryClient 单例播种/断言（WS handler 非组件路径不依赖 Provider）
    const { queryClient } = await import('@/services/query/queryClient')
    const { queryKeys } = await import('@/services/query/queryKeys')
    queryClient.setQueryData(queryKeys.longTermTasks, [
      { id: 'task-sub', title: '子任务', status: 'running' },
    ] as never)

    renderHook(() => useRealtimeEvents())

    await act(async () => {
      emitEvent('task_deleted', { task_id: 'task-sub' })
    })

    const tasks = queryClient.getQueryData<{ id: string }[]>(queryKeys.longTermTasks) ?? []
    expect(tasks.some((t) => t.id === 'task-sub')).toBe(false)
  })
})
