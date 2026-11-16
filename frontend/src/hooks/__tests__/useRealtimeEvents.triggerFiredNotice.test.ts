/**
 * @feature: FP-T12 前端适配（X24-F1/P-71 触发异常交付通知） | @ci frontend-test
 *
 * trigger_fired 前端订阅腿（后端投递腿 = triggers_ext FrontendEmitter →
 * session.emit_event，仅 delivery=failed/skipped 出帧；sent 不出帧——正常
 * 交付已由聊天注入通道可见）。收到异常交付帧即入通知中心：
 * - failed/skipped 各自文案；failure 原因非空入文案；高优 error 分类
 *   （触发器无重试无补偿，通知是用户唯一可见面，P-61 关联）；
 * - 不携 taskId 路由坐标（触发器无单实体路由面板，防点击空路由）；
 * - 降级：缺 trigger_id 或未知 delivery（含 sent）→ 不通知；
 * - 稳定 id：同帧重放按「同 id 更新不新增」去重（badge 不增长）。
 * - WS_SERVER_EVENTS.TRIGGER_FIRED 键在（单源 yaml 契约的前端镜像，
 *   WS_SERVER_EVENT_KEYS fail-closed 对账的显式断言面）。
 */
import { renderHook } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
// useRealtimeEvents 订阅真实 globalWS 单例；对 store 的副作用用真实 store 断言
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useNotificationStore } from '@/stores/notificationStore'

const emit = (event: string, frame: unknown) =>
  (globalWS as unknown as { _emit: (e: string, d: unknown) => void })._emit(event, frame)

describe('useRealtimeEvents: 触发异常交付通知（X24-F1/P-71）', () => {
  beforeEach(() => {
    useNotificationStore.getState().clearAll()
  })

  it('词汇契约：TRIGGER_FIRED 键在且值为 trigger_fired（单源 yaml 前端镜像）', () => {
    expect(WS_SERVER_EVENTS.TRIGGER_FIRED).toBe('trigger_fired')
  })

  it('delivery=failed 帧 → 高优 error 通知（触发器名+失败原因入文案）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('trigger_fired', {
        type: 'trigger_fired',
        data: {
          trigger_id: 'trigger_schedule_0123456789ab',
          name: '每日提醒',
          pipeline_id: 'pipe-1',
          delivery: 'failed',
          failure: '消息注入异常: 连接被拒绝',
        },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.id === 'trigger-fired-trigger_schedule_0123456789ab-failed')
      expect(notif).toBeDefined()
      expect(notif?.title).toBe('触发器投递失败')
      expect(notif?.message).toContain('每日提醒')
      expect(notif?.message).toContain('消息注入异常: 连接被拒绝')
      expect(notif?.category).toBe('error')
      expect(notif?.priority).toBe('high')
      // toast 面 = 瞬态通知（autoDismissMs + 非阻塞，P-129 失败通知同款）
      expect(notif?.isBlocking).toBe(false)
      expect(notif?.autoDismissMs).toBeGreaterThan(0)
      // 不携 taskId 路由坐标（触发器无单实体路由面板）
      expect(notif?.taskId).toBeUndefined()
    } finally {
      unmount()
    }
  })

  it('delivery=skipped 帧 → 通知（无 failure 降级通用文案，不出现 undefined）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('trigger_fired', {
        type: 'trigger_fired',
        data: {
          trigger_id: 'trigger_delay_0123456789ab',
          name: '',
          pipeline_id: 'pipe-2',
          delivery: 'skipped',
        },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.id === 'trigger-fired-trigger_delay_0123456789ab-skipped')
      expect(notif).toBeDefined()
      expect(notif?.title).toBe('触发器投递被跳过')
      // 缺 name → 降级短 id 文案（不伪造触发器名）
      expect(notif?.message).toContain('触发器 trigger_dela')
      expect(notif?.message).not.toContain('undefined')
      expect(notif?.category).toBe('error')
    } finally {
      unmount()
    }
  })

  it('delivery=sent 帧 → 不入通知中心（后端设计不出帧；前端防御未知交付值）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('trigger_fired', {
        type: 'trigger_fired',
        data: {
          trigger_id: 'trigger_interval_0123456789ab',
          name: '周期任务',
          pipeline_id: 'pipe-3',
          delivery: 'sent',
        },
      })
      expect(useNotificationStore.getState().notifications).toHaveLength(0)
    } finally {
      unmount()
    }
  })

  it('缺 trigger_id 或无 delivery → 不通知（无去重键/未知交付值的裸提示只是噪音）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('trigger_fired', { type: 'trigger_fired', data: { name: '无锚点', delivery: 'failed' } })
      emit('trigger_fired', { type: 'trigger_fired', data: { trigger_id: 't1' } })
      emit('trigger_fired', { type: 'trigger_fired' })
      expect(useNotificationStore.getState().notifications).toHaveLength(0)
    } finally {
      unmount()
    }
  })

  it('同帧重放（稳定 id）→ 更新不新增，badge 不增长', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      const frame = {
        type: 'trigger_fired',
        data: {
          trigger_id: 'trigger_event_0123456789ab',
          name: '重放触发器',
          pipeline_id: 'pipe-4',
          delivery: 'failed',
          failure: 'boom',
        },
      }
      emit('trigger_fired', frame)
      emit('trigger_fired', frame)
      const matches = useNotificationStore
        .getState()
        .notifications.filter((n) => n.id === 'trigger-fired-trigger_event_0123456789ab-failed')
      expect(matches).toHaveLength(1)
    } finally {
      unmount()
    }
  })
})
