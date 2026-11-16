// @feature: FP-T12 前端适配(X18-F1/P-139 task_deleted 摘除关联通知接线) | @ci: frontend-test
/**
 * X18-F1（P-139）接线腿：task_deleted 帧 → 通知中心摘除带该 taskId 的条目。
 *
 * 修复前 task_deleted 只清任务缓存，通知中心里带该 taskId 的条目残留空路由
 * 留驻到刷新。store 级契约（pruneForTarget 摘除语义/persist 面）见
 * notificationStore.prune.test.ts；本文件锁定订阅腿接线（帧到 store 的链路）。
 * useRealtimeEvents 订阅真实 globalWS 单例，副作用用真实 store 断言。
 */
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import {
  resetDismissedNotificationIdsForTest,
  useNotificationStore,
} from '@/stores/notificationStore'

const emit = (event: string, frame: unknown) =>
  (globalWS as unknown as { _emit: (e: string, d: unknown) => void })._emit(event, frame)

describe('useRealtimeEvents: task_deleted 摘除关联通知（P-139 接线腿）', () => {
  beforeEach(() => {
    useNotificationStore.getState().clearAll()
    resetDismissedNotificationIdsForTest()
  })

  it('task_deleted 帧：带该 taskId 的通知被摘除，无关条目保留', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      const { addNotification } = useNotificationStore.getState()
      addNotification({
        id: 'dead-1',
        title: '将删任务通知',
        priority: 'normal',
        category: 'info',
        isBlocking: false,
        taskId: 'task-dead',
        sourceLabel: '测试',
      })
      addNotification({
        id: 'alive-1',
        title: '无关任务通知',
        priority: 'normal',
        category: 'info',
        isBlocking: false,
        taskId: 'task-alive',
        sourceLabel: '测试',
      })

      act(() => {
        emit(WS_SERVER_EVENTS.TASK_DELETED, {
          type: 'task_deleted',
          data: { task_id: 'task-dead' },
        })
      })

      const ids = useNotificationStore.getState().notifications.map((n) => n.id)
      expect(ids).not.toContain('dead-1')
      expect(ids).toContain('alive-1')
    } finally {
      unmount()
    }
  })

  it('task_deleted 缺 task_id：不摘除（无坐标 no-op）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      useNotificationStore.getState().addNotification({
        id: 'keep-1',
        title: '保留通知',
        priority: 'normal',
        category: 'info',
        isBlocking: false,
        taskId: 'task-keep',
        sourceLabel: '测试',
      })

      act(() => {
        emit(WS_SERVER_EVENTS.TASK_DELETED, { type: 'task_deleted', data: {} })
      })

      expect(useNotificationStore.getState().notifications.map((n) => n.id)).toEqual(['keep-1'])
    } finally {
      unmount()
    }
  })
})
