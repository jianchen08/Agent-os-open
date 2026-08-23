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
//  Mock: GlobalWebSocket（真实订阅面：useRealtimeEvents 经 globalWS 订阅）
//  此前 mock 的是无人 import 的 WebSocketService（mock 空气），事件从未触达真实 hook。
// ---------------------------------------------------------------------------
const listeners: Record<string, Set<(...args: any[]) => void>> = {}

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      if (!listeners[event]) listeners[event] = new Set()
      listeners[event].add(cb)
    }),
    unsubscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      listeners[event]?.delete(cb)
    }),
    send: vi.fn(),
    sendInteractionResponse: vi.fn().mockResolvedValue(undefined),
    connect: vi.fn(),
    status: 'connected',
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
      const errorPre = document.querySelector('pre.text-status-error')
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
      // 2026-08-12 视觉改版：状态色从整卡染色迁移到 3px 左边条（aria-hidden span）
      const bar = card.querySelector('span[aria-hidden="true"]') as HTMLElement
      expect(bar).toBeInTheDocument()
      const style = bar.getAttribute('style') || ''
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
  // 附加：useRealtimeEvents 任务生命周期集成
  // （原 execution_start/done 集成用例已删除：execution_* 事件后端无发射源，
  //   订阅已随 2026-08 死接线清理移除——原用例断言的是从未真实生效的链路）
  // -----------------------------------------------------------------------
  describe('useRealtimeEvents 任务事件集成', () => {
    it('task_status_update 失败态应更新长期任务缓存并 bump 工作区版本', async () => {
      // 批次 4 query 化：tasks 数据在 query cache（queryKeys.longTermTasks），
      // 经全局 queryClient 单例播种/断言（WS handler 非组件路径不依赖 Provider）
      const { queryClient } = await import('@/services/query/queryClient')
      const { queryKeys } = await import('@/services/query/queryKeys')
      queryClient.setQueryData(queryKeys.longTermTasks, [
        { id: 'task-err', title: '失败恢复任务', status: 'running', currentPhase: 'execute' },
      ] as never)

      renderHook(() => useRealtimeEvents())

      const versionBefore = useLayoutModeStore.getState().workspaceDataVersion
      await act(async () => {
        emitEvent('task_status_update', {
          task_id: 'task-err',
          new_status: 'failed',
          error: '连接超时',
        })
      })

      const tasks = queryClient.getQueryData<{ id: string; status: string; error?: string }[]>(
        queryKeys.longTermTasks,
      ) ?? []
      const task = tasks.find((t) => t.id === 'task-err')
      expect(task).toBeDefined()
      expect(task!.status).toBe('failed')
      expect(task!.error).toBe('连接超时')
      expect(useLayoutModeStore.getState().workspaceDataVersion).toBeGreaterThan(versionBefore)
    })
  })
})
