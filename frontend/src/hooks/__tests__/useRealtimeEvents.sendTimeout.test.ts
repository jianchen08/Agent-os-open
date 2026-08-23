/**
 * @feature 错误透传 | @ci frontend-test
 *
 * user_input_send_timeout 透传链（2026-08-21 用户裁决：任何错误都必须让用户看见）：
 * WS 断线期间发送的消息排队超 TTL 被撤回时，UI 层必须——撤除对应"思考中"占位
 * 气泡、停止该管道流式态、原位置插入 system 错误消息、通知中心高优告警。
 * 此前行为是气泡无限转、刷新后凭空消失、零提示。
 *
 * 2026-08-22 单一消息数组（ADR）：乐观 user 在主数组（status='sending'），超时
 * 将其标记 failed（消息保留、位置不丢、可重试复用 cmid 幂等重发），同刻插入
 * system 错误气泡——不再有 pending 区（已退役）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

// useRealtimeEvents 订阅真实 globalWS 单例；对 store 的副作用用真实 store 断言
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useNotificationStore } from '@/stores/notificationStore'

const PIPELINE_ID = 'pipe-send-timeout-1'
const CMID = 'cmid-abc'

describe('useRealtimeEvents: user_input_send_timeout 错误透传', () => {
  beforeEach(() => {
    const ps = usePipelineMessageStore.getState()
    // 重置该管道的本地消息/流式态，隔离用例
    usePipelineMessageStore.setState({
      messagesByPipeline: { ...ps.messagesByPipeline, [PIPELINE_ID]: [] },
      streamingState: { ...ps.streamingState, [PIPELINE_ID]: undefined },
    })
    useNotificationStore.getState().clearAll()
  })

  it('超时事件应标记发送中消息 failed、停流式、插 system 错误消息、发高优通知', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      const ps = usePipelineMessageStore.getState()
      // 复现发送现场（ADR 2026-08-22 单一消息数组）：乐观 user 直接进主数组，
      // 流式态驱动"思考中"指示
      ps.startStreaming(PIPELINE_ID, CMID)
      ps.addMessage(PIPELINE_ID, {
        id: CMID,
        sessionId: 'thread-1',
        role: 'user',
        content: '测试消息',
        timestamp: new Date().toISOString(),
        status: 'sending',
        clientMessageId: CMID,
      } as never)

      // 触发超时广播（真实链路：GlobalWebSocket 排队 TTL 到点 _emit）
      ;(globalWS as unknown as { _emit: (e: string, d: unknown) => void })._emit(
        'user_input_send_timeout',
        {
          type: 'user_input_send_timeout',
          data: {
            thread_id: 'thread-1',
            pipeline_id: PIPELINE_ID,
            client_message_id: CMID,
            reason: '连接断开超过 20s，消息未送达已撤回',
          },
        },
      )

      const after = usePipelineMessageStore.getState()
      const msgs = after.getMessages(PIPELINE_ID)
      // 发送中消息原地保留并标记 failed（消息不消失、位置不丢、可重试）
      const optimistic = msgs.find((m) => m.id === CMID)
      expect(optimistic).toBeDefined()
      expect(optimistic?.status).toBe('failed')
      // 原位置插入 system 错误消息
      const err = msgs.find((m) => m.id === `send_failed_${CMID}`)
      expect(err).toBeDefined()
      expect(err?.role).toBe('system')
      expect(err?.status).toBe('error')
      expect(err?.content).toContain('消息没有发到服务器')
      // 流式态已停
      expect(after.streamingState[PIPELINE_ID]).toBeUndefined()
      // 通知中心高优告警
      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.title === '消息发送失败')
      expect(notif).toBeDefined()
      expect(notif?.priority).toBe('high')
      expect(notif?.category).toBe('error')
    } finally {
      unmount()
    }
  })

  it('未超时的其他管道消息不受影响', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      const ps = usePipelineMessageStore.getState()
      ps.addMessage(PIPELINE_ID, {
        id: CMID,
        sessionId: 'thread-1',
        role: 'user',
        content: '另一管道的发送中消息',
        timestamp: new Date().toISOString(),
        status: 'sending',
        clientMessageId: CMID,
      } as never)

      // 另一管道（pipe-other）的超时事件：cmid 与本管道占位不匹配
      ;(globalWS as unknown as { _emit: (e: string, d: unknown) => void })._emit(
        'user_input_send_timeout',
        {
          type: 'user_input_send_timeout',
          data: {
            thread_id: 'thread-2',
            pipeline_id: 'pipe-other',
            client_message_id: 'cmid-other',
            reason: 'x',
          },
        },
      )

      const after = usePipelineMessageStore.getState()
      const msgs = after.getMessages(PIPELINE_ID)
      // 本管道消息不受影响（仍 sending）
      expect(msgs.find((m) => m.id === CMID)?.status).toBe('sending')
    } finally {
      unmount()
    }
  })
})
