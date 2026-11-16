/**
 * @feature: FP-T12 前端适配（P-19/U-1 后台任务终态通知） | @ci frontend-test
 *
 * task_completed/task_cancelled/task_failed 前端订阅腿（后端投递腿 = tasks
 * 插件 frontend.emit → session.emit_event，负载 task_id/title/pipeline_id/
 * user_id/status，失败帧另含失败归因 error/failure_class/failure_reason）：
 * 收到帧即入通知中心——
 * - 完成/取消/失败各自文案与分类；taskId 携带（点击路由任务面板的既有面）；
 * - 防打扰门控：任务管道正是当前活跃管道 → 不弹（用户正看着会话收尾）；
 * - 降级：负载缺 title → 任务短 id 文案（不伪造任务名）；缺 task_id 或
 *   未知 status → 不通知；
 * - 稳定 id：同帧重放按「同 id 更新不新增」去重（badge 不增长）。
 */
import { renderHook } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
// useRealtimeEvents 订阅真实 globalWS 单例；对 store 的副作用用真实 store 断言
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

const emit = (event: string, frame: unknown) =>
  (globalWS as unknown as { _emit: (e: string, d: unknown) => void })._emit(event, frame)

describe('useRealtimeEvents: 后台任务终态通知（P-19）', () => {
  beforeEach(() => {
    useNotificationStore.getState().clearAll()
    usePipelineMessageStore.setState({ activePipelineId: null })
  })

  it('task_completed 帧 → 完成通知（任务名入文案，taskId 供点击路由任务面板）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('task_completed', {
        type: 'task_completed',
        data: {
          task_id: 'task-1',
          title: '重构登录模块',
          pipeline_id: 'task-1',
          thread_id: 'task-1',
          user_id: 'u1',
          status: 'completed',
        },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.id === 'task-terminal-task-1-completed')
      expect(notif).toBeDefined()
      expect(notif?.title).toBe('后台任务已完成')
      expect(notif?.message).toContain('重构登录模块')
      expect(notif?.category).toBe('success')
      expect(notif?.taskId).toBe('task-1')
      expect(notif?.isBlocking).toBe(false)
    } finally {
      unmount()
    }
  })

  it('task_cancelled 帧 → 取消通知（info 分类）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('task_cancelled', {
        type: 'task_cancelled',
        data: { task_id: 'task-2', title: '数据迁移', pipeline_id: 'task-2', status: 'cancelled' },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.id === 'task-terminal-task-2-cancelled')
      expect(notif).toBeDefined()
      expect(notif?.title).toBe('后台任务已取消')
      expect(notif?.message).toContain('数据迁移')
      expect(notif?.category).toBe('info')
    } finally {
      unmount()
    }
  })

  it('task_failed 帧 → 失败通知（P-129：error 分类高优，失败归因入文案，taskId 供路由）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('task_failed', {
        type: 'task_failed',
        data: {
          task_id: 'task-3',
          title: '重构登录模块',
          pipeline_id: 'task-3',
          thread_id: 'task-3',
          user_id: 'u1',
          status: 'failed',
          error: '评估未通过：3 处断言失败',
          failure_class: 'eval_rejected',
          failure_reason: '3 处断言失败',
        },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.id === 'task-terminal-task-3-failed')
      expect(notif).toBeDefined()
      expect(notif?.title).toBe('后台任务失败')
      expect(notif?.message).toContain('重构登录模块')
      expect(notif?.message).toContain('评估未通过：3 处断言失败')
      expect(notif?.category).toBe('error')
      expect(notif?.priority).toBe('high')
      expect(notif?.taskId).toBe('task-3')
      // toast 面 = 瞬态通知（autoDismissMs + 非阻塞，与 completed 同款）
      expect(notif?.isBlocking).toBe(false)
      expect(notif?.autoDismissMs).toBeGreaterThan(0)
    } finally {
      unmount()
    }
  })

  it('task_failed 帧缺归因/标题 → 失败通知降级（短 id 文案，不伪造失败原因）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('task_failed', {
        type: 'task_failed',
        data: { task_id: 'task-4', title: '', pipeline_id: 'task-4', status: 'failed' },
      })

      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.id === 'task-terminal-task-4-failed')
      expect(notif).toBeDefined()
      expect(notif?.message).toContain('任务 task-4')
      expect(notif?.message).not.toContain('undefined')
      expect(notif?.category).toBe('error')
    } finally {
      unmount()
    }
  })

  it('防打扰门控：任务管道是当前活跃管道 → 不弹通知（用户正看着会话收尾）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      usePipelineMessageStore.setState({ activePipelineId: 'task-live' })
      emit('task_completed', {
        type: 'task_completed',
        data: { task_id: 'task-live', title: '在看任务', pipeline_id: 'task-live', status: 'completed' },
      })
      expect(
        useNotificationStore.getState().notifications.find((n) => n.taskId === 'task-live'),
      ).toBeUndefined()
    } finally {
      unmount()
    }
  })

  it('负载缺 title → 降级为任务短 id 文案（不伪造任务名）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('task_completed', {
        type: 'task_completed',
        data: { task_id: '686a7736-7f46-44a0-8827-b9f05603063b', title: '', status: 'completed' },
      })
      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.taskId === '686a7736-7f46-44a0-8827-b9f05603063b')
      expect(notif).toBeDefined()
      expect(notif?.message).toContain('任务 686a7736-7f4')
      expect(notif?.message).not.toContain('undefined')
    } finally {
      unmount()
    }
  })

  it('缺 task_id / 未知 status → 不通知（无路由无去重键的裸提示只是噪音）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      emit('task_completed', { type: 'task_completed', data: { title: '无锚点', status: 'completed' } })
      emit('task_completed', { type: 'task_completed', data: { task_id: 't9', status: 'archived' } })
      emit('task_completed', { type: 'task_completed' })
      expect(useNotificationStore.getState().notifications).toHaveLength(0)
    } finally {
      unmount()
    }
  })

  it('同帧重放（稳定 id）→ 更新不新增，badge 不增长', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    try {
      const frame = {
        type: 'task_completed',
        data: { task_id: 'task-r', title: '重放任务', pipeline_id: 'task-r', status: 'completed' },
      }
      emit('task_completed', frame)
      emit('task_completed', frame)
      const matches = useNotificationStore
        .getState()
        .notifications.filter((n) => n.id === 'task-terminal-task-r-completed')
      expect(matches).toHaveLength(1)
    } finally {
      unmount()
    }
  })
})
