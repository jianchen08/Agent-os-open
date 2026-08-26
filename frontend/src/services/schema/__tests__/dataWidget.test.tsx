/**
 * A1a：数据 widget 数据获取测试（datasourceUri + 数据形状协议）
 *
 * 覆盖：normalizeRows/series/scalar 形状归一；useDataWidget 挂载取数
 * （绝对 URI 直连 / 信封解开 / 失败回退静态）；Chart/Table/StatusCard
 * 三组件 datasourceUri 接线（无 uri 时零行为变化）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import {
  normalizeDataPayload,
  normalizeRows,
  normalizeSeries,
  normalizeScalar,
  useDataWidget,
} from '@/services/schema/dataWidget'
import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'

const apiGet = vi.fn()
const apiCall = vi.fn()
const { wsSubscribe, wsUnsubscribe } = vi.hoisted(() => ({
  wsSubscribe: vi.fn(),
  wsUnsubscribe: vi.fn(),
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { subscribe: wsSubscribe, unsubscribe: wsUnsubscribe },
}))
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiCall(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

beforeEach(() => {
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: {} })
  apiCall.mockReset()
  apiCall.mockResolvedValue({ data: { ok: true } })
})

describe('数据形状协议', () => {
  it('rows：{columns,rows} 原样 / 裸数组取首行生成列 / 信封解开', () => {
    const a = normalizeRows({
      columns: [{ key: 'id', label: 'ID' }],
      rows: [{ id: 1, name: 'x' }],
    })
    expect(a.columns[0].key).toBe('id')
    expect(a.rows).toEqual([{ id: 1, name: 'x' }])

    const b = normalizeRows([{ id: 2, name: 'y' }])
    expect(b.columns.map((c) => c.key).sort()).toEqual(['id', 'name'])

    const c = normalizeDataPayload({ data: [{ id: 3 }] }, 'rows') as ReturnType<typeof normalizeRows>
    expect((c as { rows: unknown[] }).rows).toHaveLength(1)
  })

  it('series：{labels,datasets} 原样归一 / 裸数值数组 → 单序列', () => {
    const a = normalizeSeries({
      labels: ['a', 'b'],
      datasets: [{ data: [1, 2], label: 's' }],
    })
    expect(a.labels).toEqual(['a', 'b'])
    expect(a.datasets[0].data).toEqual([1, 2])

    const b = normalizeSeries([3, 4, 5])
    expect(b.datasets).toHaveLength(1)
    expect(b.datasets[0].data).toEqual([3, 4, 5])
  })

  it('scalar：对象原样 / 裸值包成 {value}', () => {
    expect(normalizeScalar({ value: 42, progress: 66 })).toEqual({ value: 42, progress: 66 })
    expect(normalizeScalar('hello')).toEqual({ value: 'hello' })
  })
})

describe('useDataWidget', () => {
  it('无 uri → 静态 data 直接返回、不发请求', () => {
    function Host({ uri }: { uri?: string }) {
      const r = useDataWidget(uri ? { datasourceUri: uri, data: [{ id: 1 }] } : { data: [{ id: 1 }] }, 'rows')
      return <div data-testid="r">{JSON.stringify(r.data)}</div>
    }
    const { rerender } = render(<Host />)
    expect(screen.getByTestId('r').textContent).toContain('id')
    expect(apiGet).not.toHaveBeenCalled()
    rerender(<Host uri="/api/v1/tools" />)
    expect(apiGet).toHaveBeenCalledWith('/api/v1/tools')
  })

  it('有 uri → 挂载取数并归一（信封 {data} 解开）', async () => {
    apiGet.mockResolvedValue({ data: { data: [{ id: 9, name: '九' }] } })
    function Host() {
      const r = useDataWidget({ datasourceUri: '/api/v1/tools' }, 'rows')
      return (
        <div data-testid="r">
          {JSON.stringify(r.data)}
          <span data-testid="loading">{String(r.loading)}</span>
        </div>
      )
    }
    render(<Host />)
    await waitFor(() =>
      expect(screen.getByTestId('r').textContent).toContain('id'),
    )
  })

  it('失败 → error 且回退静态 data', async () => {
    apiGet.mockRejectedValue(new Error('boom'))
    function Host() {
      const r = useDataWidget({ datasourceUri: '/ext/x', data: [{ id: 1 }] }, 'rows')
      return (
        <div>
          <span data-testid="err">{r.error}</span>
          <span data-testid="d">{JSON.stringify(r.data)}</span>
        </div>
      )
    }
    render(<Host />)
    await waitFor(() => expect(screen.getByTestId('err').textContent).toContain('boom'))
    expect(screen.getByTestId('d').textContent).toContain('id')
  })

  it('轮询重拉（reloadKey 变化）：保留已渲染数据、不闪 loading（有旧值静默刷新）', async () => {
    let resolveFetch: ((v: { data: { value: number } }) => void) | undefined
    apiGet.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve
        }),
    )
    function Host({ reloadKey = 0 }: { reloadKey?: number }) {
      const r = useDataWidget({ datasourceUri: '/ext/x' }, 'scalar', reloadKey)
      return (
        <div>
          <span data-testid="v">{JSON.stringify((r.data as { value?: number } | undefined)?.value)}</span>
          <span data-testid="loading">{String(r.loading)}</span>
        </div>
      )
    }
    const { rerender } = render(<Host />)
    expect(screen.getByTestId('loading').textContent).toBe('true')
    resolveFetch?.({ data: { value: 1 } })
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('1'))
    expect(screen.getByTestId('loading').textContent).toBe('false')
    // 重拉：有旧值 → 不闪 loading、旧数据保留，直到新值到达
    rerender(<Host reloadKey={1} />)
    expect(screen.getByTestId('loading').textContent).toBe('false')
    expect(screen.getByTestId('v').textContent).toBe('1')
    resolveFetch?.({ data: { value: 2 } })
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('2'))
  })

  it('非绝对 URI → 走 /api/v1/datasource 代理', async () => {
    apiGet.mockResolvedValue({ data: [{ id: 1 }] })
    function Host() {
      useDataWidget({ datasourceUri: 'monitoring/tasks' }, 'rows')
      return null
    }
    render(<Host />)
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/api/v1/datasource/monitoring/tasks'),
    )
  })
})

describe('组件接线（datasourceUri）', () => {
  it('ChartWidget：datasourceUri → series 数据渲染', async () => {
    apiGet.mockResolvedValue({
      data: { labels: ['A', 'B'], datasets: [{ data: [3, 7] }] },
    })
    render(<ChartWidget chartType="bar" datasourceUri="/ext/monitoring/trend" />)
    // 数据到达 → 空态消失、真实图表柱渲染
    await waitFor(() => expect(screen.queryByText('暂无图表数据')).not.toBeInTheDocument())
    expect(document.querySelector('svg rect')).toBeTruthy()
  })

  it('TableWidget：datasourceUri → 行数据渲染单元格', async () => {
    apiGet.mockResolvedValue({
      data: { columns: [{ key: 'id', label: 'ID' }], rows: [{ id: 't-1' }] },
    })
    render(<TableWidget datasourceUri="/ext/monitoring/tasks" />)
    await waitFor(() => expect(screen.getByText('t-1')).toBeInTheDocument())
  })

  it('StatusCardWidget：datasourceUri → scalar value 渲染', async () => {
    apiGet.mockResolvedValue({ data: { value: 88 } })
    render(<StatusCardWidget datasourceUri="/ext/cost/status" label="预算" />)
    // label+数值 → progress 形态（百分比）
    await waitFor(() => expect(screen.getByText('88%')).toBeInTheDocument())
  })

  it('无 uri：三个组件零行为变化（静态 props 照常）', () => {
    render(<ChartWidget chartType="bar" data={{ labels: ['x'], datasets: [{ data: [1] }] }} />)
    expect(document.querySelector('svg')).toBeTruthy()
    render(
      <TableWidget
        columns={[{ key: 'id', label: 'ID' }]}
        data={[{ id: 1 }]}
      />,
    )
    // 列头 + 单元格都渲染（避免对裸数字 '1' 的多元素匹配）
    expect(screen.getByText('ID')).toBeInTheDocument()
    render(<StatusCardWidget label="静态" value={5} />)
    // value + label → progress 形态，进度以百分比显示
    expect(screen.getByText('5%')).toBeInTheDocument()
  })
})

// ── A1c：WS 事件驱动数据源 ─────────────────────────────────
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useWsDataSource } from '@/services/schema/dataWidget'

describe('A1c：WS 事件驱动数据源', () => {
  beforeEach(() => {
    wsSubscribe.mockClear()
    wsUnsubscribe.mockClear()
    globalThis.wsHandler = undefined as ((p: unknown) => void) | undefined
    wsSubscribe.mockImplementation((_ch: string, h: (p: unknown) => void) => {
      globalThis.wsHandler = h
    })
  })

  it('useDataWidget + refresh:{type:ws,channel}：事件驱动更新（不走 loading）', async () => {
    apiGet.mockRejectedValue(new Error('http 不应被调用'))
    function Host() {
      const r = useDataWidget({ refresh: { type: 'ws', channel: 'cost_update' } }, 'scalar')
      return <span data-testid="v">{JSON.stringify((r.data as { value?: number } | undefined)?.value)}</span>
    }
    render(<Host />)
    expect(apiGet).not.toHaveBeenCalled()
    // 模拟 WS 推送事件 → scalar 归一（{value:...} 形状）→ 更新
    ;(globalThis.wsHandler as (p: unknown) => void)?.({ value: 42 })
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('42'))
  })

  it('useWsDataSource：独立 hook 订阅/退订', async () => {
    function Host() {
      const { data } = useWsDataSource({ channel: 'cost_update', initial: 0 })
      return <span data-testid="w">{String(data)}</span>
    }
    const { unmount } = render(<Host />)
    expect(wsSubscribe).toHaveBeenCalledWith('cost_update', expect.any(Function))
    ;(globalThis.wsHandler as (p: unknown) => void)?.(7)
    await waitFor(() => expect(screen.getByTestId('w').textContent).toBe('7'))
    unmount()
    expect(wsUnsubscribe).toHaveBeenCalledWith('cost_update', expect.any(Function))
  })
})

// ── A1b：TableWidget 行操作（rowActions） ──────────────────

describe('TableWidget 行操作（rowActions）', () => {
  it('声明 rowActions → 操作列渲染，点击 fetch 并重拉数据', async () => {
    apiCall.mockResolvedValue({ data: { ok: true } })
    // 列表数据
    apiGet.mockResolvedValue({
      data: { columns: [{ key: 'id', label: 'ID' }], rows: [{ id: 't-1' }, { id: 't-2' }] },
    })
    render(
      <TableWidget
        datasourceUri="/ext/trigger_setup_tool/triggers"
        rowActions={[
          { key: 'trigger', label: '触发', url: '/ext/trigger_setup_tool/triggers/{id}/trigger' },
        ]}
      />,
    )
    await waitFor(() => expect(screen.getAllByTestId('row-action-trigger').length).toBeGreaterThan(0))
    apiCall.mockClear()
    const getsBefore = apiGet.mock.calls.length
    fireEvent.click(screen.getAllByTestId('row-action-trigger')[0])
    await waitFor(() => expect(apiCall).toHaveBeenCalled())
    const [cfg] = apiCall.mock.calls[0]
    expect(cfg.method).toBe('POST')
    expect(cfg.url).toBe('/ext/trigger_setup_tool/triggers/t-1/trigger')
    // 成功后 reloadKey 重拉
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(getsBefore))
  })

  it('confirm 声明：取消则不请求', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    apiCall.mockResolvedValue({ data: { ok: true } })
    apiGet.mockResolvedValue({ data: { columns: [{ key: 'id', label: 'ID' }], rows: [{ id: 'x' }] } })
    render(
      <TableWidget
        datasourceUri="/ext/triggers"
        rowActions={[{ key: 'del', label: '删除', url: '/ext/triggers/{id}', method: 'DELETE', confirm: '确认？' }]}
      />,
    )
    await waitFor(() => expect(screen.getByTestId('row-action-del')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('row-action-del'))
    await new Promise((r) => setTimeout(r, 50))
    expect(apiCall).not.toHaveBeenCalled()
  })
})

describe('StatusCard valueKey（A2 成本卡前置）', () => {
  it('datasourceUri + valueKey 取嵌套字段渲染', async () => {
    apiGet.mockResolvedValue({
      data: { global_stats: { daily_usage_percent: 62, daily_tokens: 100 } },
    })
    render(
      <StatusCardWidget
        label="今日用量"
        datasourceUri="/ext/cost_control/usage/statistics"
        valueKey="global_stats.daily_usage_percent"
      />,
    )
    await waitFor(() => expect(screen.getByText('62%')).toBeInTheDocument())
  })
})
