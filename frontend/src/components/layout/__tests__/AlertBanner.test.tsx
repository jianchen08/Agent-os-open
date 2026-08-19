/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：AlertBanner 异常浮现提示条 + useLayoutAlerts（task_layout_responsive 任务 2）
 *
 * 推演链：DSH「无常驻底栏」理念 → 决策「状态栏无常驻，异常时浮现提示条」→ 功能点：
 * - 无异常时不渲染（视觉干净，内容区 +22px）
 * - 连接断开 / 审批待处理时浮现，可点击处理
 * - 异常解除后延迟几秒自动收起
 * - useLayoutAlerts 从 layoutModeStore 派生告警项
 * - budget：cost_control /budget/status alert_level 过阈出现（治理债清理 7.5.3）
 */

import { act, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import type { BudgetStatusResponse } from '@/services/api/costControl'
import { AlertBanner, useLayoutAlerts, type AlertBannerItem } from '../AlertBanner'

// useLayoutAlerts 的 budget 源是 cost_control getBudgetStatus（经 useBudgetStatus）。
// mock 掉网络层，用可变 holder 控制各用例的 alert_level。
const mockBudget = vi.hoisted(
  () => ({ current: null as import('@/services/api/costControl').BudgetStatusResponse | null }),
)
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => mockBudget.current,
}))

const CONNECTED = {
  state: 'connected' as const,
  latencyMs: 5,
  reconnectAttempt: 0,
  lastConnectedAt: null,
  queuedMessages: 0,
}

function makeAlert(over: Partial<AlertBannerItem> = {}): AlertBannerItem {
  return { id: 'conn', kind: 'connection', message: '内核连接已断开', tone: 'error', ...over }
}

/** 构造 BudgetStatus（alert_level 判定在后端 budget_manager，前端按值消费） */
function makeBudget(alert_level: string, usage_percent = 85): BudgetStatusResponse {
  const limit = 100000
  const used = Math.round((limit * usage_percent) / 100)
  return {
    scope: 'global',
    limit,
    used,
    remaining: limit - used,
    usage_percent,
    alert_level,
    estimated_cost: 1.5,
  }
}

describe('AlertBanner — 异常浮现提示条', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED },
      pendingInteractions: [],
    })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('无异常时不渲染任何横幅', () => {
    render(<AlertBanner alerts={[]} />)
    expect(screen.queryByTestId('alert-banner')).not.toBeInTheDocument()
  })

  it('异常时浮现横幅（role=alert），文案可见', () => {
    render(<AlertBanner alerts={[makeAlert()]} />)
    expect(screen.getByTestId('alert-banner')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('内核连接已断开')
  })

  it('多条异常并列浮现', () => {
    render(
      <AlertBanner
        alerts={[
          makeAlert(),
          makeAlert({ id: 'approval', kind: 'approval', message: '有 2 项审批待处理' }),
        ]}
      />,
    )
    expect(screen.getAllByRole('alert')).toHaveLength(2)
    expect(screen.getByText('有 2 项审批待处理')).toBeInTheDocument()
  })

  it('异常解除后延迟自动收起（默认 4s）', () => {
    const { rerender } = render(<AlertBanner alerts={[makeAlert()]} />)
    expect(screen.getByTestId('alert-banner')).toBeInTheDocument()

    // 异常解除 → 横幅保留几秒（提示已恢复），随后自动消失
    rerender(<AlertBanner alerts={[]} />)
    expect(screen.getByTestId('alert-banner')).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(4000)
    })
    expect(screen.queryByTestId('alert-banner')).not.toBeInTheDocument()
  })

  it('点击横幅回调 onAction（跳转详情）', () => {
    const onAction = vi.fn()
    const item = makeAlert({ actionLabel: '查看监控' })
    render(<AlertBanner alerts={[item]} onAction={onAction} />)

    act(() => {
      screen.getByTestId('alert-banner').click()
    })
    expect(onAction).toHaveBeenCalledTimes(1)
    expect(onAction).toHaveBeenCalledWith(item)
  })
})

describe('useLayoutAlerts — 从 layoutModeStore 派生告警', () => {
  beforeEach(() => {
    mockBudget.current = null
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED },
      pendingInteractions: [],
    })
  })

  it('连接正常且无审批 → 空告警', () => {
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(0)
  })

  it('断开/失败 → connection 告警', () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 3 },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(1)
    expect(result.current[0].kind).toBe('connection')
    expect(result.current[0].tone).toBe('error')
  })

  it('审批待处理 → approval 告警（含数量）', () => {
    useLayoutModeStore.setState({
      pendingInteractions: [
        {
          id: 'i1',
          executionId: 'e1',
          prompt: '允许执行命令',
          timestamp: '2026-01-01T00:00:00Z',
        },
        {
          id: 'i2',
          executionId: 'e2',
          prompt: '允许写文件',
          timestamp: '2026-01-01T00:00:00Z',
        },
      ],
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current[0].kind).toBe('approval')
    expect(result.current[0].message).toContain('2')
  })

  it('连接 + 审批同时异常 → 两条告警', () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'failed' },
      pendingInteractions: [
        {
          id: 'i1',
          executionId: 'e1',
          prompt: 'p',
          timestamp: '2026-01-01T00:00:00Z',
        },
      ],
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current.map((a) => a.kind).sort()).toEqual(['approval', 'connection'])
  })
})

describe('useLayoutAlerts budget — cost_control 预算告警（治理债清理 7.5.3）', () => {
  beforeEach(() => {
    mockBudget.current = null
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED },
      pendingInteractions: [],
    })
  })
  afterEach(() => {
    mockBudget.current = null
  })

  it('alert_level=warning → budget 告警出现（warning tone，含使用率）', async () => {
    mockBudget.current = makeBudget('warning', 85.4)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {}) // flush getBudgetStatus promise → setState

    const budgetItems = result.current.filter((a) => a.kind === 'budget')
    expect(budgetItems).toHaveLength(1)
    expect(budgetItems[0].id).toBe('budget')
    expect(budgetItems[0].tone).toBe('warning')
    expect(budgetItems[0].message).toContain('85')
    expect(budgetItems[0].message).toContain('接近限额')
    expect(budgetItems[0].actionLabel).toBe('查看成本')
  })

  it('alert_level=info → 不出现 budget 告警', async () => {
    mockBudget.current = makeBudget('info', 50)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current).toHaveLength(0)
  })

  it('alert_level=critical → error tone', async () => {
    mockBudget.current = makeBudget('critical', 92)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    const budgetItems = result.current.filter((a) => a.kind === 'budget')
    expect(budgetItems).toHaveLength(1)
    expect(budgetItems[0].tone).toBe('error')
    expect(budgetItems[0].message).toContain('92')
  })

  it('exhausted 覆盖 warning：cost_update 复查后只留一条 error', async () => {
    // 先 warning 出一条
    mockBudget.current = makeBudget('warning', 85)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})
    expect(result.current.filter((a) => a.kind === 'budget')).toHaveLength(1)
    expect(result.current.filter((a) => a.kind === 'budget')[0].tone).toBe('warning')

    // 预算耗尽 → cost_update 事件到达 → 复查 → 同 id 覆盖，仅一条 error
    mockBudget.current = makeBudget('exhausted', 101)
    act(() => {
      ;(globalWS as unknown as { _emit: (event: string, data: unknown) => void })._emit(
        WS_SERVER_EVENTS.COST_UPDATE,
        { type: 'cost_update' },
      )
    })
    await act(async () => {})

    const budgetItems = result.current.filter((a) => a.kind === 'budget')
    expect(budgetItems).toHaveLength(1)
    expect(budgetItems[0].tone).toBe('error')
    expect(budgetItems[0].id).toBe('budget')
    expect(budgetItems[0].message).toContain('预算已耗尽')
  })

  it('exhausted 单值起始 → 仅一条 budget 告警（不 warning+error 双条）', async () => {
    mockBudget.current = makeBudget('exhausted', 100)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current).toHaveLength(1)
    expect(result.current[0].kind).toBe('budget')
    expect(result.current[0].tone).toBe('error')
  })

  it('预算状态获取失败/无数据 → 不出现 budget 告警（fail-open 不打扰）', async () => {
    mockBudget.current = null
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current).toHaveLength(0)
  })

  it('budget 与 connection 并存 → 两条告警', async () => {
    mockBudget.current = makeBudget('warning', 85)
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 2 },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current.map((a) => a.kind).sort()).toEqual(['budget', 'connection'])
  })
})
