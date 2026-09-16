// @feature: FP-0.2.四 前端Schema驱动 | @ci: frontend-test
/**
 * ChartWidget 离屏暂停端到端测试
 *
 * 监控 tab 非激活（display:none）/ 窗口最小化 → 图表 WS 推送不触发重渲染，
 * 可见即恢复（缓存帧应用）。jsdom 无 IntersectionObserver，用可触发替身驱动。
 * 场景带初始静态数据：图表根 div（IO 观察目标）自挂载起存在，与真实监控
 * 图表（先有数据后离屏）一致；空态分支无根 div，走 fail-open 不参与暂停。
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ChartWidget } from '../ChartWidget'
import { resetSharedFetchCache } from '@/services/schema/dataWidget'

let ioCb: IntersectionObserverCallback | null = null
let ioTargets: Element[] = []

function stubIntersectionObserver() {
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(cb: IntersectionObserverCallback) {
        ioCb = cb
      }
      observe(target: Element) {
        ioTargets.push(target)
      }
      unobserve() {}
      disconnect() {}
    },
  )
}

function fireVisible(visible: boolean) {
  act(() => {
    ioCb?.(
      ioTargets.map((target) => ({ target, isIntersecting: visible }) as unknown as IntersectionObserverEntry),
      {} as IntersectionObserver,
    )
  })
}

const apiGet = vi.fn()
const { wsSubscribe } = vi.hoisted(() => ({ wsSubscribe: vi.fn() }))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { subscribe: wsSubscribe, unsubscribe: vi.fn() },
}))
vi.mock('@/services/api/client', () => ({
  default: Object.assign(vi.fn(), { get: (...args: unknown[]) => apiGet(...args) }),
}))

beforeEach(() => {
  ioCb = null
  ioTargets = []
  wsSubscribe.mockReset()
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: {} })
  resetSharedFetchCache()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ChartWidget 离屏暂停', () => {
  it('不可见时 WS 推送不更新渲染；恢复可见应用缓存帧', () => {
    stubIntersectionObserver()
    render(
      <ChartWidget
        chartType="bar"
        datasourceUri="/api/v1/datasource/monitor/x"
        data={{ labels: ['甲', '乙'], datasets: [{ data: [1, 2] }] }}
        refresh={{ type: 'ws', channel: 'monitor.chart' }}
      />,
    )
    // 初始数据已渲染
    expect(screen.getByText('甲')).toBeInTheDocument()
    // 订阅了声明通道
    expect(wsSubscribe).toHaveBeenCalledWith('monitor.chart', expect.any(Function))
    const push = wsSubscribe.mock.calls[0][1] as (p: unknown) => void

    // 面板被隐藏
    fireVisible(false)
    act(() => {
      push({ labels: ['丙', '丁'], datasets: [{ data: [7, 8] }] })
    })
    // 隐藏期间停留在旧数据（新推送未触发重渲染）
    expect(screen.getByText('甲')).toBeInTheDocument()
    expect(screen.queryByText('丙')).not.toBeInTheDocument()

    // 恢复可见：缓存帧立即应用
    fireVisible(true)
    expect(screen.getByText('丙')).toBeInTheDocument()
    expect(screen.queryByText('甲')).not.toBeInTheDocument()
  })

  it('可见时 WS 推送即渲染（在线行为不变）', () => {
    render(
      <ChartWidget
        chartType="line"
        datasourceUri="/api/v1/datasource/monitor/y"
        refresh={{ type: 'ws', channel: 'monitor.chart' }}
      />,
    )
    expect(screen.getByText('暂无图表数据')).toBeInTheDocument()
    const push = wsSubscribe.mock.calls[0][1] as (p: unknown) => void
    act(() => {
      push({ labels: ['一', '二'], datasets: [{ data: [5, 6] }] })
    })
    expect(screen.queryByText('暂无图表数据')).not.toBeInTheDocument()
    expect(screen.getByText('二')).toBeInTheDocument()
  })
})
