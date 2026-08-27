/**
 * useWidgetEvents 全局 widget_event 订阅 hook 测试
 *
 * 验证：挂载时订阅 WIDGET_EVENT + 解析派发到 widgetEventStore；
 * 无效事件（adaptWidgetEvent 返回 null）不派发；卸载时取消订阅。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

const { mockSubscribe, mockUnsubscribe, mockHandlers } = vi.hoisted(() => ({
  mockSubscribe: vi.fn(),
  mockUnsubscribe: vi.fn(),
  mockHandlers: new Map<string, (data: any) => void>(),
}))

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (e: string, h: (d: any) => void) => {
      mockSubscribe(e, h)
      mockHandlers.set(e, h)
    },
    unsubscribe: (e: string, h: (d: any) => void) => {
      mockUnsubscribe(e, h)
      if (mockHandlers.get(e) === h) mockHandlers.delete(e)
    },
  },
}))

vi.mock('@/services/websocket/MessageAdapter', () => ({
  adaptWidgetEvent: (raw: any) => {
    if (!raw || raw.type !== WS_SERVER_EVENTS.WIDGET_EVENT) return null
    const d = raw.data
    if (!d || typeof d !== 'object') return null
    return {
      widgetId: d.widget_id,
      event: d.event,
      payload: d.payload,
      sequence: d.sequence,
    }
  },
}))

vi.mock('@/utils/logger', () => ({
  loggers: { websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

const mockDispatch = vi.fn()
vi.mock('@/stores/widgetEventStore', () => ({
  useWidgetEventStore: (selector: (s: any) => any) => selector({ dispatchWidgetEvent: mockDispatch }),
}))

import { useWidgetEvents } from '@/hooks/useWidgetEvents'

describe('useWidgetEvents', () => {
  beforeEach(() => {
    mockSubscribe.mockClear()
    mockUnsubscribe.mockClear()
    mockHandlers.clear()
    mockDispatch.mockClear()
  })

  it('挂载时订阅 widget_event；有效事件解析后派发到 store', () => {
    const { unmount } = renderHook(() => useWidgetEvents())

    expect(mockSubscribe).toHaveBeenCalledWith(
      WS_SERVER_EVENTS.WIDGET_EVENT,
      expect.any(Function),
    )

    const handler = mockHandlers.get(WS_SERVER_EVENTS.WIDGET_EVENT)!
    handler({
      type: WS_SERVER_EVENTS.WIDGET_EVENT,
      data: { widget_id: 'wid-1', event: 'snapshot', payload: { x: 1 }, sequence: 7 },
    })
    expect(mockDispatch).toHaveBeenCalledWith({
      widgetId: 'wid-1',
      event: 'snapshot',
      payload: { x: 1 },
      sequence: 7,
    })

    unmount()
  })

  it('无效事件（非 widget_event / 缺 data）→ 不派发', () => {
    renderHook(() => useWidgetEvents())
    const handler = mockHandlers.get(WS_SERVER_EVENTS.WIDGET_EVENT)!

    handler({ type: 'other_event', data: {} })
    handler({ type: WS_SERVER_EVENTS.WIDGET_EVENT, data: null })
    expect(mockDispatch).not.toHaveBeenCalled()
  })

  it('卸载时取消订阅', () => {
    const { unmount } = renderHook(() => useWidgetEvents())
    const handler = mockHandlers.get(WS_SERVER_EVENTS.WIDGET_EVENT)!
    unmount()

    expect(mockUnsubscribe).toHaveBeenCalledWith(WS_SERVER_EVENTS.WIDGET_EVENT, handler)
  })
})
