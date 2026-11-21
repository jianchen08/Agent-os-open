// @feature FP-T12 前端适配(批次5 通知出队冲刷消费面) | @ci: frontend-test
/**
 * notify_flush 帧消费测试（批次 5：管道空闲即冲刷）。
 *
 * notify_judge 判定 worthy 打标晚于 new_message 帧（post 时序错位），内核
 * 空闲期补发 notify_flush 帧 → useRealtimeEvents 消费 → systemNotifier 弹出。
 * 契约：
 * - notify_flush 帧到达即弹（notifyWorthy=true，dedupeKey=message_id 与帧
 *   链路共用幂等记账）；
 * - 同 message_id 重复帧（押单后的重放/补发）不双弹；
 * - body 空（无摘要）静默降级（notifyPipelineMessage 空正文契约）；
 * - 卸载退订干净。
 *
 * mock 仅限网络/WS/壳通知边界（globalWS、showSystemNotification）。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

const listeners: Record<string, Set<(...args: unknown[]) => void>> = {}
const connectMock = vi.fn()
const subscribeMock = vi.fn((event: string, cb: (...args: unknown[]) => void) => {
  if (!listeners[event]) listeners[event] = new Set()
  listeners[event].add(cb)
})
const unsubscribeMock = vi.fn((event: string, cb: (...args: unknown[]) => void) => {
  listeners[event]?.delete(cb)
})
const wasKickedMock = vi.fn(() => false)

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, cb: (...args: unknown[]) => void) => subscribeMock(event, cb),
    unsubscribe: (event: string, cb: (...args: unknown[]) => void) => unsubscribeMock(event, cb),
    connect: (...args: unknown[]) => connectMock(...args),
    wasKickedByReplacement: () => wasKickedMock(),
    get status() {
      return 'connected'
    },
  },
}))

vi.mock('@/services/auth/tokenLifecycle', () => ({
  ensureFreshToken: vi.fn(),
}))

const showSystemNotificationMock = vi.hoisted(() => vi.fn(async () => true))
vi.mock('@/utils/systemNotification', () => ({
  showSystemNotification: (...args: unknown[]) => showSystemNotificationMock(...args),
}))

import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'

function emit(event: string, payload: unknown) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of [...cbs]) cb(payload)
}

beforeEach(() => {
  for (const key of Object.keys(listeners)) delete listeners[key]
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('notify_flush 帧消费（批次 5 空闲冲刷）', () => {
  it('帧到达立即弹系统通知（notify_worthy=true 直通，dedupeKey=message_id）', async () => {
    renderHook(() => useRealtimeEvents())
    await act(async () => {
      emit(WS_SERVER_EVENTS.NOTIFY_FLUSH, {
        data: {
          message_id: 'm_flush_1',
          pipeline_id: 'pipe-1',
          thread_id: 'sess-flush-1',
          notify_worthy: true,
          title: '报告搞定了，已经发你邮箱。',
        },
      })
    })
    expect(showSystemNotificationMock).toHaveBeenCalledTimes(1)
    expect(showSystemNotificationMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '灵汐',
        body: '报告搞定了，已经发你邮箱。',
        threadId: 'sess-flush-1',
      }),
    )
  })

  it('同 message_id 重复帧不双弹（押单后的重放/补发共用幂等记账）', async () => {
    renderHook(() => useRealtimeEvents())
    const frame = {
      data: {
        message_id: 'm_flush_dup',
        pipeline_id: 'pipe-1',
        thread_id: 'sess-flush-1',
        notify_worthy: true,
        title: '搞定了第一段。',
      },
    }
    await act(async () => {
      emit(WS_SERVER_EVENTS.NOTIFY_FLUSH, frame)
      emit(WS_SERVER_EVENTS.NOTIFY_FLUSH, frame)
    })
    expect(showSystemNotificationMock).toHaveBeenCalledTimes(1)
  })

  it('body 空（无摘要）静默降级不弹（空正文契约）', async () => {
    renderHook(() => useRealtimeEvents())
    await act(async () => {
      emit(WS_SERVER_EVENTS.NOTIFY_FLUSH, {
        data: { message_id: 'm_flush_empty', notify_worthy: true, title: '   ' },
      })
    })
    expect(showSystemNotificationMock).not.toHaveBeenCalled()
  })

  it('卸载退订干净（NOTIFY_FLUSH 不残留监听）', async () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    const before = listeners[WS_SERVER_EVENTS.NOTIFY_FLUSH]?.size ?? 0
    expect(before).toBe(1)
    unmount()
    expect(listeners[WS_SERVER_EVENTS.NOTIFY_FLUSH]?.size ?? 0).toBe(0)
  })
})
