// @feature: FP-T12 前端适配(segment事件对账) | @ci: frontend-test
// @feature: message-segments P1 | @ci: frontend-test
/**
 * segment_activated ack 对账（消息段模型：‹i/n› 切换 / 多端激活的收尾）。
 *
 * 契约（[来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md §5.1]）：
 * 复用压缩对账 compressionReconcileTimers 防抖模式——防抖合并、坐标缺失跳过、
 * 对账 = 消息全量重拉 + 段清单刷新 + 段视图标记处置；失败弹通知且标记保留
 * （乐观视图与权威序列的分叉须可重试收敛）。
 *
 * 标记处置（2026-10-21 用户实证修订）：ack 段 = 本地标记 → **保留**（标记=「当前
 * 在看第几代」，清掉会把 ‹/› 落点重置回最新归档代 → 同一代内容与当前代逐字节
 * 相同（重试后模型给同样回复）时永远翻到同一代）；ack 段 ≠ 本地标记（他端切换
 * /本地乐观态失效）→ 照旧清除。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

const listeners: Record<string, Set<(...args: unknown[]) => void>> = {}

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, cb: (...args: never[]) => void) => {
      if (!listeners[event]) listeners[event] = new Set()
      listeners[event].add(cb)
    }),
    unsubscribe: vi.fn((event: string, cb: (...args: never[]) => void) => {
      listeners[event]?.delete(cb)
    }),
    connect: vi.fn(),
    wasKickedByReplacement: vi.fn(() => false),
    status: 'connected',
  },
}))

import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

function emit(event: string, payload: unknown) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of [...cbs]) cb(payload)
}

/** 挂载 + 桩定 fetchMessages / fetchSegments，返回 spies 与 unmount */
function mountWithSpies(opts?: { fetchRejected?: boolean }) {
  const fetchMessagesSpy = opts?.fetchRejected
    ? vi.fn().mockRejectedValue(new Error('network down'))
    : vi.fn().mockResolvedValue(undefined)
  const fetchSegmentsSpy = vi.fn().mockResolvedValue(undefined)
  usePipelineMessageStore.setState({
    fetchMessages: fetchMessagesSpy,
    fetchSegments: fetchSegmentsSpy,
  } as never)
  const { unmount } = renderHook(() => useRealtimeEvents())
  return { fetchMessagesSpy, fetchSegmentsSpy, unmount }
}

beforeEach(() => {
  for (const key of Object.keys(listeners)) delete listeners[key]
  vi.clearAllMocks()
  useNotificationStore.getState().clearAll()
  usePipelineMessageStore.setState({
    segmentViewByPipeline: { 'pipe-seg': { baseSeq: 50, segmentId: 'seg-gen2' } },
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useRealtimeEvents — segment_activated 对账', () => {
  it('ack 事件 → 防抖后全量重拉消息 + 刷新段清单，成功后清乐观段视图标记', async () => {
    vi.useFakeTimers()
    const { fetchMessagesSpy, fetchSegmentsSpy, unmount } = mountWithSpies()

    // 内核 emit_event 线上形态：会话坐标在 data._threadId（无 thread_id 键）
    act(() =>
      emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, {
        data: { pipeline_id: 'pipe-seg', segment_id: 'seg-gen1', _threadId: 'sess-seg' },
      }),
    )
    // 防抖窗口内不拉（替换落库与事件发出存在竞态窗口）
    expect(fetchMessagesSpy).not.toHaveBeenCalled()
    expect(fetchSegmentsSpy).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(fetchMessagesSpy).toHaveBeenCalledWith('pipe-seg', { threadId: 'sess-seg' })
    expect(fetchSegmentsSpy).toHaveBeenCalledWith('pipe-seg')
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-seg']).toBeUndefined()
    unmount()
  })

  it('插件 frontend.emit 变体（data.thread_id 坐标）同样触发对账', async () => {
    vi.useFakeTimers()
    const { fetchMessagesSpy, unmount } = mountWithSpies()

    act(() =>
      emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, {
        data: { pipeline_id: 'pipe-seg', thread_id: 'sess-seg' },
      }),
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(fetchMessagesSpy).toHaveBeenCalledWith('pipe-seg', { threadId: 'sess-seg' })
    unmount()
  })

  it('同管道连续 ack 防抖合并为一次对账', async () => {
    vi.useFakeTimers()
    const { fetchMessagesSpy, unmount } = mountWithSpies()

    act(() => {
      emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, {
        data: { pipeline_id: 'pipe-seg', thread_id: 'sess-seg' },
      })
      emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, {
        data: { pipeline_id: 'pipe-seg', thread_id: 'sess-seg' },
      })
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(fetchMessagesSpy).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('坐标缺失（无 pipeline_id/thread_id）不触发对账', async () => {
    vi.useFakeTimers()
    const { fetchMessagesSpy, unmount } = mountWithSpies()

    act(() => emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, { data: { pipeline_id: '' } }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(fetchMessagesSpy).not.toHaveBeenCalled()
    unmount()
  })

  it('对账失败弹通知且乐观标记保留（分叉须可重试收敛）', async () => {
    vi.useFakeTimers()
    const { unmount } = mountWithSpies({ fetchRejected: true })

    act(() =>
      emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, {
        data: { pipeline_id: 'pipe-seg', thread_id: 'sess-seg' },
      }),
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '对话版本切换对账失败')
    expect(notif).toBeDefined()
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-seg']).toBeDefined()
    unmount()
  })

  it('ack 段与本地段视图标记一致 → 标记保留（‹/› 落点不回退，防「永远翻到同一代」死循环）', async () => {
    // 用户实证：重试后模型给出与当前代逐字节相同的回复，‹ 落点那一代内容与
    // 当前代相同 → 看着没变；若 ack 后清掉标记，viewOrdinal 归零，下一次 ‹
    // 又落回同一代 → 永远在同一代之间打转。ack 段 = 本地标记时保留标记
    // （=「当前在看第几代」），前翻才能继续走到更早的代。
    vi.useFakeTimers()
    const { unmount } = mountWithSpies()

    act(() =>
      emit(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, {
        data: { pipeline_id: 'pipe-seg', segment_id: 'seg-gen2', _threadId: 'sess-seg' },
      }),
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-seg']).toEqual({
      baseSeq: 50,
      segmentId: 'seg-gen2',
    })
    unmount()
  })

  it('卸载后退订 segment_activated（无残留 handler）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    expect((listeners[WS_SERVER_EVENTS.SEGMENT_ACTIVATED] ?? new Set()).size).toBeGreaterThan(0)
    unmount()
    expect((listeners[WS_SERVER_EVENTS.SEGMENT_ACTIVATED] ?? new Set()).size).toBe(0)
  })
})
