/**
 * ErrorRecoveryFlow.test.tsx
 *
 * 验证 AC-1l: 错误恢复流程跑通（工具失败→恢复→重试→完成）
 *
 * 测试覆盖：
 * 1. 工具失败显示
 * 2. 失败后重试成功
 * 3. 多次重试
 * 4. Stream 错误恢复
 * 5. 执行取消
 * 6. 部分失败（多工具调用中一个失败）
 */

import { act, render, renderHook, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { ActivityData, ActivityStatus } from '@/types/activity'

// ---------------------------------------------------------------------------
//  Mock: lucide-react
// ---------------------------------------------------------------------------
vi.mock('lucide-react', () => {
  const icons = [
    'Loader2',
    'CheckCircle2',
    'XCircle',
    'AlertTriangle',
    'Ban',
    'Play',
    'ChevronDown',
    'ChevronRight',
    'Copy',
    'RefreshCw',
    'Clock',
    'Sparkles',
    'Target',
    'Wrench',
  ]
  const m: Record<string, any> = {}
  for (const name of icons) {
    m[name] = (p: any) => <svg data-testid={`icon-${name}`} {...p} />
  }
  return m
})

// ---------------------------------------------------------------------------
//  Mock: @/lib/utils
// ---------------------------------------------------------------------------
vi.mock('@/lib/utils', () => ({
  cn: (...args: (string | undefined | null | false)[]) => args.filter(Boolean).join(' '),
}))

// ---------------------------------------------------------------------------
//  Mock: confirm dialog (used by ActivityCard)
// ---------------------------------------------------------------------------
vi.mock('@/utils/confirm', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn().mockResolvedValue(true),
    dialogState: { open: false, message: '', onConfirm: vi.fn(), onCancel: vi.fn() },
    setDialogState: vi.fn(),
  }),
}))

// ---------------------------------------------------------------------------
//  Mock: formatDuration from activity types
// ---------------------------------------------------------------------------
vi.mock('@/types/activity', async (importOriginal) => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
  const actual = await importOriginal<typeof import('@/types/activity')>()
  return {
    ...actual,
    formatDuration: (ms: number) => {
      if (ms < 1000) return `${ms}ms`
      const seconds = Math.floor(ms / 1000)
      if (seconds < 60) return `${seconds}s`
      const minutes = Math.floor(seconds / 60)
      const remainingSeconds = seconds % 60
      return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`
    },
  }
})

// ---------------------------------------------------------------------------
//  Mock: WebSocket 服务 (用于 useRealtimeEvents 集成测试)
// ---------------------------------------------------------------------------
const listeners: Record<string, Set<(...args: any[]) => void>> = {}

vi.mock('@/services/websocket/WebSocketService', () => ({
  webSocketService: {
    subscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      if (!listeners[event]) listeners[event] = new Set()
      listeners[event].add(cb)
    }),
    unsubscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      listeners[event]?.delete(cb)
    }),
  },
}))

vi.mock('@/constants/websocket', () => ({
  WS_SERVER_EVENTS: {
    STREAM_START: 'stream_start',
    STREAM_CHUNK: 'stream_chunk',
    STREAM_END: 'stream_end',
    STREAM_ERROR: 'stream_error',
    EXECUTION_START: 'execution_start',
    EXECUTION_PROGRESS: 'execution_progress',
    EXECUTION_OUTPUT: 'execution_output',
    EXECUTION_DONE: 'execution_done',
    EXECUTION_CANCELLED: 'execution_cancelled',
    SUB_AGENT_CREATED: 'sub_agent_created',
    SUB_AGENT_WAITING_INPUT: 'sub_agent_waiting_input',
    SUB_AGENT_COMPLETED: 'sub_agent_completed',
    WORKFLOW_STEP_UPDATE: 'workflow_step_update',
  },
  WebSocketStatus: {
    DISCONNECTED: 'disconnected',
    CONNECTING: 'connecting',
    CONNECTED: 'connected',
  },
}))

// ---------------------------------------------------------------------------
//  工厂函数
// ---------------------------------------------------------------------------

/** 创建 ActivityData */
function createActivityData(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: `activity-${Math.random().toString(36).slice(2, 9)}`,
    title: 'Test Activity',
    status: 'running',
    details: [],
    actions: [],
    ...overrides,
  }
}

/** 触发 WebSocket 事件 */
function emitEvent(event: string, data: Record<string, unknown>) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of cbs) cb(data)
}

/** 渲染多张 ActivityCard */
function renderActivities(items: ActivityData[]) {
  return render(
    <div>
      {items.map((a) => (
        <ActivityCard key={a.id} activity={a} />
      ))}
    </div>,
  )
}

// ---------------------------------------------------------------------------
//  测试
// ---------------------------------------------------------------------------

describe('ErrorRecoveryFlow — AC-1l: 错误恢复流程', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    for (const key of Object.keys(listeners)) delete listeners[key]
    useLayoutModeStore.setState({
      activeExecutions: [],
      pendingInteractions: [],
    })
  })

  // -----------------------------------------------------------------------
  // 1. 工具失败显示
  // -----------------------------------------------------------------------
  describe('工具失败显示', () => {
    it('ActivityCard 显示 failed 状态和错误信息', () => {
      const data = createActivityData({
        id: 'deploy-1',
        title: 'deploy',
        status: 'failed',
        error: '连接超时',
      })

      render(<ActivityCard activity={data} defaultExpanded />)

      // 通过 data-activity-status 查找
      const failedCard = document.querySelector('[data-activity-status="failed"]')
      expect(failedCard).toBeInTheDocument()

      // 错误信息可见（defaultExpanded = true 时展开显示 error）
      expect(screen.getByText('错误')).toBeInTheDocument()
      expect(screen.getByText('连接超时')).toBeInTheDocument()

      // 红色样式
      const errorPre = document.querySelector('pre.text-red-600')
      expect(errorPre).toBeInTheDocument()
    })

    it('failed 状态卡片使用红色主题 CSS 变量', () => {
      const data = createActivityData({
        title: 'deploy',
        status: 'failed',
        error: '超时',
      })
      render(<ActivityCard activity={data} />)

      const card = document.querySelector('[data-activity-status="failed"]') as HTMLElement
      expect(card).toBeInTheDocument()
      // CSS 变量包含 --accent-error
      const style = card.getAttribute('style') || ''
      expect(style).toContain('--accent-error')
    })
  })

  // -----------------------------------------------------------------------
  // 2. 失败后重试成功
  // -----------------------------------------------------------------------
  describe('失败后重试成功', () => {
    it('第一次 failed，第二次 completed', () => {
      const failed = createActivityData({
        id: 'deploy-v1',
        title: 'deploy-v1',
        status: 'failed',
        error: '连接超时',
      })
      const success = createActivityData({
        id: 'deploy-v2',
        title: 'deploy-v2',
        status: 'completed',
      })

      renderActivities([failed, success])

      // 两个卡片
      const failedCard = document.querySelector('[data-activity-status="failed"]')
      const completedCard = document.querySelector('[data-activity-status="completed"]')
      expect(failedCard).toBeInTheDocument()
      expect(completedCard).toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // 3. 多次重试
  // -----------------------------------------------------------------------
  describe('多次重试', () => {
    it('失败 → 失败 → 成功：三张 ActivityCard 按序渲染', () => {
      const retry1 = createActivityData({
        id: 'retry-1',
        title: 'deploy',
        status: 'failed',
        error: '超时',
      })
      const retry2 = createActivityData({
        id: 'retry-2',
        title: 'deploy',
        status: 'failed',
        error: '拒绝连接',
      })
      const retry3 = createActivityData({
        id: 'retry-3',
        title: 'deploy',
        status: 'completed',
      })

      renderActivities([retry1, retry2, retry3])

      const cards = document.querySelectorAll('[data-activity-status]')
      expect(cards).toHaveLength(3)

      // 前两个 failed
      expect(cards[0].getAttribute('data-activity-status')).toBe('failed')
      expect(cards[1].getAttribute('data-activity-status')).toBe('failed')
      // 最后一个 completed
      expect(cards[2].getAttribute('data-activity-status')).toBe('completed')
    })
  })

  // -----------------------------------------------------------------------
  // 4. Stream 错误恢复
  // -----------------------------------------------------------------------
  describe('Stream 错误恢复', () => {
    it('stream 错误后新的流正常工作', () => {
      // 模拟第一次流失败
      const failedStream = createActivityData({
        id: 'stream-1',
        title: 'stream',
        status: 'failed',
        error: '流中断',
        partialOutput: ['部分数据...'],
      })

      // 模拟恢复后成功
      const recoveredStream = createActivityData({
        id: 'stream-2',
        title: 'stream',
        status: 'completed',
      })

      renderActivities([failedStream, recoveredStream])

      // 失败流
      const failedEl = document.querySelector('[data-activity-id="stream-1"]')
      expect(failedEl).toBeInTheDocument()

      // 恢复流
      const recoveredEl = document.querySelector('[data-activity-id="stream-2"]')
      expect(recoveredEl).toBeInTheDocument()
      expect(recoveredEl!.getAttribute('data-activity-status')).toBe('completed')
    })
  })

  // -----------------------------------------------------------------------
  // 5. 执行取消
  // -----------------------------------------------------------------------
  describe('执行取消', () => {
    it('cancelled 状态存在且 progress 已渲染', () => {
      const data = createActivityData({
        id: 'exec-cancel',
        title: 'long-task',
        status: 'cancelled',
        error: '用户取消',
        progress: 60,
      })

      render(<ActivityCard activity={data} defaultExpanded />)

      // cancelled 状态
      const card = document.querySelector('[data-activity-status="cancelled"]')
      expect(card).toBeInTheDocument()

      // 取消原因可见（error 字段，defaultExpanded 时展开）
      expect(screen.getByText('用户取消')).toBeInTheDocument()

      // 进度条渲染（60%）
      const progressBar = document.querySelector('[style*="width: 60%"]')
      expect(progressBar).toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // 6. 部分失败（多工具调用中一个失败）
  // -----------------------------------------------------------------------
  describe('部分失败', () => {
    it('三个工具调用中第二个失败，其余成功', () => {
      const toolA = createActivityData({
        id: 'tool-a',
        title: 'tool-a',
        toolName: 'tool-a',
        status: 'completed',
      })
      const toolB = createActivityData({
        id: 'tool-b',
        title: 'tool-b',
        toolName: 'tool-b',
        status: 'failed',
        error: '权限不足',
      })
      const toolC = createActivityData({
        id: 'tool-c',
        title: 'tool-c',
        toolName: 'tool-c',
        status: 'completed',
      })

      renderActivities([toolA, toolB, toolC])

      // 三张卡片
      const cards = document.querySelectorAll('[data-activity-status]')
      expect(cards).toHaveLength(3)

      // 状态验证
      expect(cards[0].getAttribute('data-activity-status')).toBe('completed')
      expect(cards[1].getAttribute('data-activity-status')).toBe('failed')
      expect(cards[2].getAttribute('data-activity-status')).toBe('completed')
    })
  })

  // -----------------------------------------------------------------------
  // 附加：useRealtimeEvents 执行错误恢复集成
  // -----------------------------------------------------------------------
  describe('useRealtimeEvents 执行错误恢复集成', () => {
    it('execution_start → execution_done(failed) → execution_start → execution_done(success)', () => {
      renderHook(() => useRealtimeEvents())

      // 第一次执行 → 失败
      act(() => {
        emitEvent('execution_start', {
          execution_id: 'exec-1',
          execution_type: 'tool',
          name: 'deploy',
        })
      })
      let state = useLayoutModeStore.getState()
      expect(state.activeExecutions).toHaveLength(1)
      expect(state.activeExecutions[0].status).toBe('running')

      act(() => {
        emitEvent('execution_done', {
          execution_id: 'exec-1',
          success: false,
          error: '连接超时',
        })
      })
      state = useLayoutModeStore.getState()
      expect(state.activeExecutions[0].status).toBe('failed')

      // 第二次执行 → 成功
      act(() => {
        emitEvent('execution_start', {
          execution_id: 'exec-2',
          execution_type: 'tool',
          name: 'deploy',
        })
      })
      state = useLayoutModeStore.getState()
      expect(state.activeExecutions).toHaveLength(2)

      act(() => {
        emitEvent('execution_done', {
          execution_id: 'exec-2',
          success: true,
        })
      })
      state = useLayoutModeStore.getState()
      expect(state.activeExecutions[0].status).toBe('failed')
      expect(state.activeExecutions[1].status).toBe('completed')
    })
  })
})
