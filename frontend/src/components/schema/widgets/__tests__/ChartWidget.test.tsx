/**
 * @feature FP-0.2.可观测性 图表 widget 渲染契约 | @ci frontend-test
 *
 * ChartWidget 单测：七种图表类型的 SVG 渲染、数据源回退（datasourceUri →
 * props.data）、加载/错误/空数据三态、图例与标题、坐标标签截断。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const useDataWidgetMock = vi.hoisted(() => vi.fn())
vi.mock('@/services/schema/dataWidget', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useDataWidget: (...args: unknown[]) => useDataWidgetMock(...args),
}))

import { ChartWidget } from '../ChartWidget'

const DATA = {
  labels: ['一月', '二月', '三月'],
  datasets: [
    { label: '收入', data: [10, 30, 20] },
    { label: '支出', data: [5, 12, 18] },
  ],
}

function svgCounts() {
  const svg = document.querySelector('svg')
  return {
    svg: svg != null,
    path: document.querySelectorAll('path').length,
    rect: document.querySelectorAll('rect').length,
    circle: document.querySelectorAll('circle').length,
    polygon: document.querySelectorAll('polygon').length,
  }
}

beforeEach(() => {
  useDataWidgetMock.mockReset()
  useDataWidgetMock.mockReturnValue({ data: null, error: null, loading: false })
})

afterEach(() => {
  cleanup()
})

describe('ChartWidget', () => {
  it('静态数据 + 各图表类型：SVG 分型渲染且互有判别元素', () => {
    const expectations: Array<[string, (c: ReturnType<typeof svgCounts>) => boolean]> = [
      ['line', (c) => c.path > 0],
      ['area', (c) => c.path > 0],
      ['bar', (c) => c.rect > 0],
      ['pie', (c) => c.path > 0 || c.circle > 0],
      ['doughnut', (c) => c.path > 0 || c.circle > 0],
      ['radar', (c) => c.polygon > 0],
      ['scatter', (c) => c.circle > 0],
      ['mystery-type', (c) => c.rect > 0], // 未知类型回退 bar
    ]
    for (const [type, ok] of expectations) {
      cleanup()
      const { container } = render(<ChartWidget chartType={type} data={DATA} />)
      expect(container.querySelector('svg'), `chartType=${type}`).toBeTruthy()
      expect(ok(svgCounts()), `chartType=${type} 应有判别 SVG 元素`).toBe(true)
    }
  })

  it('标题、尺寸与图例（多数据集）渲染', () => {
    render(<ChartWidget chartType="bar" data={DATA} title="月度收支" width={640} height={320} showLegend />)
    expect(screen.getByText('月度收支')).toBeTruthy()
    const holder = document.querySelector('div[style]')
    expect(holder?.getAttribute('style')).toContain('640')
    expect(screen.getByText('收入')).toBeTruthy()
    expect(screen.getByText('支出')).toBeTruthy()
  })

  it('单数据集不渲染图例；showLegend=false 也不渲染', () => {
    const single = { labels: ['a', 'b'], datasets: [{ label: '唯一', data: [1, 2] }] }
    const { rerender } = render(<ChartWidget chartType="bar" data={single} />)
    expect(screen.queryByText('唯一')).toBeNull()

    rerender(<ChartWidget chartType="bar" data={DATA} showLegend={false} />)
    expect(screen.queryByText('收入')).toBeNull()
  })

  it('超长标签截断为 6 字符加省略号', () => {
    render(
      <ChartWidget
        chartType="line"
        data={{ labels: ['2026-01-01'], datasets: [{ data: [1] }] }}
      />,
    )
    expect(screen.getByText('2026-0…')).toBeTruthy()
  })

  it('空数据：渲染占位与「暂无图表数据」，非 loading 态', () => {
    render(<ChartWidget chartType="bar" data={{ labels: [], datasets: [] }} />)
    expect(screen.getByText('暂无图表数据')).toBeTruthy()
  })

  it('无数据 + loading：只显示加载态不显示占位文案', () => {
    useDataWidgetMock.mockReturnValue({ data: null, error: null, loading: true })
    render(<ChartWidget chartType="bar" data={{ labels: [], datasets: [] }} />)
    expect(screen.queryByText('暂无图表数据')).toBeNull()
  })

  it('远程错误：透传 DataWidgetStatus 错误态', () => {
    useDataWidgetMock.mockReturnValue({ data: null, error: '上游 502', loading: false })
    render(<ChartWidget chartType="bar" data={DATA} />)
    expect(screen.getByText('上游 502')).toBeTruthy()
  })

  it('datasourceUri 存在时消费远程数据而非 props.data', () => {
    useDataWidgetMock.mockReturnValue({
      data: { labels: ['远A', '远B'], datasets: [{ label: '远程', data: [7, 8] }] },
      error: null,
      loading: false,
    })
    render(
      <ChartWidget chartType="bar" data={{ labels: ['本A'], datasets: [{ data: [1] }] }} datasourceUri="monitoring://token" />,
    )
    // useDataWidget 以 (props, 'series') 形状调用
    const call = useDataWidgetMock.mock.calls[0] as unknown as [Record<string, unknown>, string]
    expect(call[1]).toBe('series')
    expect(call[0].datasourceUri).toBe('monitoring://token')
    // 远程数据胜出：单数据集无图例（本地 props.data 会渲染出两数据集图例）
    expect(screen.queryByText('收入')).toBeNull()
  })

  it('非对象/异常数据形态不崩（回退空图表占位）', () => {
    for (const bad of [null, 'str', 42, { labels: 'not-array', datasets: null }]) {
      cleanup()
      render(<ChartWidget chartType="bar" data={bad} />)
      expect(screen.getByText('暂无图表数据')).toBeTruthy()
    }
  })
})
