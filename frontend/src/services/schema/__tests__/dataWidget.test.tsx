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
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => Promise.resolve({ data: {} }),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

beforeEach(() => {
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: {} })
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
    await waitFor(() => expect(screen.getByText('88')).toBeInTheDocument())
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
