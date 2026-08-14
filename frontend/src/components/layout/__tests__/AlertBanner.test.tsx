/**
 * 功能测试：AlertBanner 异常浮现提示条 + useLayoutAlerts（task_layout_responsive 任务 2）
 *
 * 推演链：DSH「无常驻底栏」理念 → 决策「状态栏无常驻，异常时浮现提示条」→ 功能点：
 * - 无异常时不渲染（视觉干净，内容区 +22px）
 * - 连接断开 / 审批待处理时浮现，可点击处理
 * - 异常解除后延迟几秒自动收起
 * - useLayoutAlerts 从 layoutModeStore 派生告警项
 */

import { act, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { AlertBanner, useLayoutAlerts, type AlertBannerItem } from '../AlertBanner'

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
