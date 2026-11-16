/**
 * @feature: FP-0.2.route_error回执 route_error 回执消费 | @ci frontend-test
 *
 * route_error 回执消费面（ADR 2026-09-29 回执契约的前端半边）：
 * 内核对非 user_input 入站帧的拒绝（regenerate 目标缺失、segment_activate
 * 运行中等）回 route_error 帧——此前前端零消费，拒绝完全静默（装机 2026-10-16
 * 实证：回退/编辑重发被内核拒绝后用户侧无任何反馈）。收到即通知中心高优
 * 错误告警，任何路由拒绝都必须让用户看见。
 */
import { renderHook } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
// useRealtimeEvents 订阅真实 globalWS 单例；对 store 的副作用用真实 store 断言
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useNotificationStore } from '@/stores/notificationStore'

const emit = (frame: unknown) =>
  (globalWS as unknown as { _emit: (e: string, d: unknown) => void })._emit(
    'route_error',
    frame,
  )

describe('useRealtimeEvents: route_error 回执消费', () => {
  beforeEach(() => {
    useNotificationStore.getState().clearAll()
  })

  it('route_error 帧应产生高优错误通知并携带内核拒绝原因', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit({
        type: 'route_error',
        data: {
          message_type: 'regenerate',
          thread_id: 'thread-1',
          client_message_id: '',
          error: 'regenerate 未找到目标 user 消息: 686a7736-7f46-44a0-8827-b9f05603063b',
        },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.title === '操作未被服务器接受')
      expect(notif).toBeDefined()
      expect(notif?.priority).toBe('high')
      expect(notif?.category).toBe('error')
      expect(notif?.message).toContain('regenerate')
      expect(notif?.message).toContain('未找到目标 user 消息')
    } finally {
      unmount()
    }
  })

  it('缺 data 信封的退化帧也应可见（不静默丢弃）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit({ type: 'route_error' })
      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.title === '操作未被服务器接受')
      expect(notif).toBeDefined()
      expect(notif?.category).toBe('error')
    } finally {
      unmount()
    }
  })
})
