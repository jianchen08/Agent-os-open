/**
 * SubAgentFlow.test.tsx
 *
 * 验证 AC-1k: 子Agent事件链正确显示（创建→等待→完成）
 *
 * 测试覆盖：
 * 1. 完整 L3 Agent 生命周期（running → waiting_input → completed）
 * 2. 多级 Agent 层级（L1/L2/L3 Badge + path）
 * 3. Agent 失败场景
 * 4. 并行子Agent独立状态管理
 * 5. useRealtimeEvents 集成（layoutModeStore 联动）
 * 6. SubAgentCard 三种显示模式（collapsed / summary / full）
 */

import { act, render, renderHook, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SubAgentCard } from '@/components/chat/SubAgentCard'
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { SubAgentData } from '@/components/chat/SubAgentCard'

// ---------------------------------------------------------------------------
//  Mock: lucide-react
// ---------------------------------------------------------------------------
vi.mock('lucide-react', () => {
  const icons = ['ChevronDown', 'ChevronRight', 'ExternalLink', 'MessageSquare']
  const m: Record<string, any> = {}
  for (const name of icons) {
    m[name] = (p: any) => <svg data-testid={`icon-${name}`} {...p} />
  }
  return m
})

// ---------------------------------------------------------------------------
//  Mock: UI 原子组件
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/badge', () => ({
  Badge: ({ children, ...props }: any) => (
    <span data-testid="badge" data-variant={props.variant}>
      {children}
    </span>
  ),
}))

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, ...props }: any) => (
    <button data-testid="button" onClick={onClick} {...props}>
      {children}
    </button>
  ),
}))

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
    connect: vi.fn(),
    status: 'connected',
  },
}))

// ---------------------------------------------------------------------------
//  工厂函数
// ---------------------------------------------------------------------------

/** 创建 SubAgentData */
function createSubAgentData(overrides: Partial<SubAgentData> = {}): SubAgentData {
  return {
    id: 'agent-1',
    name: 'Test Agent',
    agentLevel: 3,
    status: 'running',
    ...overrides,
  }
}

/** 触发 WebSocket 事件 */
function emitEvent(event: string, data: Record<string, unknown>) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of cbs) cb(data)
}

// ---------------------------------------------------------------------------
//  测试
// ---------------------------------------------------------------------------

describe('SubAgentFlow — AC-1k: 子Agent事件链正确显示', () => {
  beforeEach(() => {
    for (const key of Object.keys(listeners)) delete listeners[key]
    useLayoutModeStore.setState({
      activeExecutions: [],
      pendingInteractions: [],
    })
  })

  // -----------------------------------------------------------------------
  // 1. 完整 L3 Agent 生命周期
  // -----------------------------------------------------------------------
  describe('完整 L3 Agent 生命周期', () => {
    it('running → waiting_input → completed 状态转换正确', () => {
      const base = createSubAgentData({
        id: 'agent-1',
        name: 'Code Writer',
        agentLevel: 3,
      })

      // ---- running ----
      const { rerender } = render(
        <SubAgentCard data={{ ...base, status: 'running' }} mode="collapsed" />,
      )
      const runningCard = screen.getByTitle('Code Writer - running')
      expect(runningCard).toBeInTheDocument()
      // running 状态图标 ●
      expect(runningCard.textContent).toContain('●')

      // ---- waiting_input ----
      rerender(
        <SubAgentCard data={{ ...base, status: 'waiting_input' }} mode="collapsed" />,
      )
      const waitingCard = screen.getByTitle('Code Writer - waiting_input')
      expect(waitingCard).toBeInTheDocument()
      expect(waitingCard.textContent).toContain('💬')

      // ---- completed ----
      rerender(
        <SubAgentCard
          data={{ ...base, status: 'completed', summary: '任务完成' }}
          mode="collapsed"
        />,
      )
      const completedCard = screen.getByTitle('Code Writer - completed')
      expect(completedCard).toBeInTheDocument()
      expect(completedCard.textContent).toContain('✓')
    })

    it('每个状态使用正确的颜色 class', () => {
      const cases = [
        { status: 'running' as const, expectedClass: 'text-primary' },
        { status: 'waiting_input' as const, expectedClass: 'text-warning' },
        { status: 'completed' as const, expectedClass: 'text-success' },
        { status: 'failed' as const, expectedClass: 'text-destructive' },
      ]

      for (const { status, expectedClass } of cases) {
        const data = createSubAgentData({ status })
        const { unmount } = render(
          <SubAgentCard data={data} mode="collapsed" />,
        )
        const card = screen.getByTitle(`Test Agent - ${status}`)
        // 颜色 class 在 span 内
        expect(card.innerHTML).toContain(expectedClass)
        unmount()
      }
    })
  })

  // -----------------------------------------------------------------------
  // 2. 多级 Agent 层级
  // -----------------------------------------------------------------------
  describe('多级 Agent 层级', () => {
    it('Badge 显示 L1 / L2 / L3', () => {
      const levels: Array<SubAgentData['agentLevel']> = [1, 2, 3]

      for (const level of levels) {
        const data = createSubAgentData({ agentLevel: level })
        const { unmount } = render(<SubAgentCard data={data} mode="collapsed" />)

        const badges = screen.getAllByTestId('badge')
        const badge = badges.find((b) => b.textContent === `L${level}`)
        expect(badge).toBeTruthy()
        unmount()
      }
    })

    it('full 模式下 path 显示层级路径', () => {
      const data = createSubAgentData({
        agentLevel: 3,
        path: ['Orchestrator', 'Code Writer', 'Test Runner'],
      })
      render(<SubAgentCard data={data} mode="full" />)

      // full 模式: data.path.join(' → ')
      expect(screen.getByText('Orchestrator → Code Writer → Test Runner')).toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // 3. Agent 失败场景
  // -----------------------------------------------------------------------
  describe('Agent 失败场景', () => {
    it('status=failed 时图标为 ✕ 并使用 text-destructive 颜色', () => {
      const data = createSubAgentData({ status: 'failed' })
      render(<SubAgentCard data={data} mode="collapsed" />)

      const card = screen.getByTitle('Test Agent - failed')
      expect(card).toBeInTheDocument()
      expect(card.textContent).toContain('✕')
      expect(card.innerHTML).toContain('text-destructive')
    })
  })

  // -----------------------------------------------------------------------
  // 4. 并行子Agent
  // -----------------------------------------------------------------------
  describe('并行子Agent', () => {
    it('两个 SubAgentCard 独立管理状态', () => {
      const agentA = createSubAgentData({ id: 'agent-a', name: 'Agent A', status: 'running' })
      const agentB = createSubAgentData({ id: 'agent-b', name: 'Agent B', status: 'running' })

      const { rerender } = render(
        <div>
          <SubAgentCard data={agentA} mode="collapsed" />
          <SubAgentCard data={agentB} mode="collapsed" />
        </div>,
      )

      expect(screen.getByTitle('Agent A - running')).toBeInTheDocument()
      expect(screen.getByTitle('Agent B - running')).toBeInTheDocument()

      // agent-a 完成
      const completedA = { ...agentA, status: 'completed' as const, summary: '完成' }
      rerender(
        <div>
          <SubAgentCard data={completedA} mode="collapsed" />
          <SubAgentCard data={agentB} mode="collapsed" />
        </div>,
      )

      expect(screen.getByTitle('Agent A - completed')).toBeInTheDocument()
      expect(screen.getByTitle('Agent B - running')).toBeInTheDocument()

      // agent-b 失败
      const failedB = { ...agentB, status: 'failed' as const }
      rerender(
        <div>
          <SubAgentCard data={completedA} mode="collapsed" />
          <SubAgentCard data={failedB} mode="collapsed" />
        </div>,
      )

      expect(screen.getByTitle('Agent A - completed')).toBeInTheDocument()
      expect(screen.getByTitle('Agent B - failed')).toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // 5. useRealtimeEvents 集成
  //    （sub_agent_created/waiting_input/completed 事件后端无发射源，订阅已于
  //     2026-08 死接线清理删除——原三个用例断言的是从未真实生效的链路，见
  //     useRealtimeEvents.ts 头部注释。此处改为守护「不再复活死接线」+ 真实
  //     事件链验证。）
  // -----------------------------------------------------------------------
  describe('useRealtimeEvents 集成', () => {
    it('sub_agent_* 死事件不再写入 activeExecutions（无发射源，防止死接线复活）', () => {
      renderHook(() => useRealtimeEvents())

      act(() => {
        emitEvent('sub_agent_created', {
          agentId: 'agent-rt-1',
          agentName: 'Realtime Agent',
          agentLevel: 3,
          parentAgentId: 'orchestrator',
        })
      })

      // 后端（kernel + 插件 event-bus）从不发射 sub_agent_created；
      // 若此断言失败说明有人重新挂上了无人投递的订阅
      expect(useLayoutModeStore.getState().activeExecutions).toHaveLength(0)
    })

    it('task_deleted 事件经 globalWS 真实订阅链路更新缓存', async () => {
      // 批次 4 query 化：tasks 数据在 query cache（queryKeys.longTermTasks），
      // 经全局 queryClient 单例播种/断言（WS handler 非组件路径不依赖 Provider）
      const { queryClient } = await import('@/services/query/queryClient')
      const { queryKeys } = await import('@/services/query/queryKeys')
      queryClient.setQueryData(queryKeys.longTermTasks, [
        { id: 'task-sub', title: '子任务', status: 'running' },
      ] as never)

      renderHook(() => useRealtimeEvents())

      await act(async () => {
        emitEvent('task_deleted', { task_id: 'task-sub' })
      })

      const tasks = queryClient.getQueryData<{ id: string }[]>(queryKeys.longTermTasks) ?? []
      expect(tasks.some((t) => t.id === 'task-sub')).toBe(false)
    })
  })

  // -----------------------------------------------------------------------
  // 6. SubAgentCard 显示模式
  // -----------------------------------------------------------------------
  describe('SubAgentCard 显示模式', () => {
    const baseData = createSubAgentData({
      name: 'Mode Test',
      summary: '这是摘要',
      path: ['Root', 'Mode Test'],
      updatedAt: new Date().toISOString(),
    })

    it('collapsed 模式：紧凑行内显示', () => {
      const { container } = render(
        <SubAgentCard data={baseData} mode="collapsed" />,
      )
      const el = container.firstElementChild as HTMLElement
      // collapsed 使用 inline-flex
      expect(el.className).toContain('inline-flex')
      // 包含名称和 Badge
      expect(screen.getByText('Mode Test')).toBeInTheDocument()
      expect(screen.getByTestId('badge')).toBeInTheDocument()
    })

    it('summary 模式：可展开/收缩', async () => {
      const user = userEvent.setup()
      render(<SubAgentCard data={baseData} mode="summary" expandable />)

      // 默认收缩 — summary 不可见
      expect(screen.queryByText('这是摘要')).not.toBeInTheDocument()

      // 点击名称触发展开
      await user.click(screen.getByText('Mode Test'))

      // 展开后显示 path 和 summary
      expect(screen.getByText(/路径:.*Root → Mode Test/)).toBeInTheDocument()
      expect(screen.getByText('这是摘要')).toBeInTheDocument()
    })

    it('full 模式：完整信息 + 操作按钮', () => {
      const onOpenDetail = vi.fn()
      render(
        <SubAgentCard data={baseData} mode="full" onOpenDetail={onOpenDetail} />,
      )

      // 完整信息直接可见
      expect(screen.getByText('Mode Test')).toBeInTheDocument()
      expect(screen.getByText('这是摘要')).toBeInTheDocument()
      expect(screen.getByText('Root → Mode Test')).toBeInTheDocument()

      // 操作按钮
      expect(screen.getByText('查看对话')).toBeInTheDocument()
    })

    it('summary 模式 onExpand 回调被触发', async () => {
      const user = userEvent.setup()
      const onExpand = vi.fn()
      render(
        <SubAgentCard
          data={baseData}
          mode="summary"
          expandable
          onExpand={onExpand}
        />,
      )

      await user.click(screen.getByText('Mode Test'))
      expect(onExpand).toHaveBeenCalledTimes(1)
    })
  })
})
