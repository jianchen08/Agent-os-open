/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ChartWidget 残余覆盖补测：
 * - 雷达图维度不足（<3 维）→ 显式提示而非空白/畸形图
 * - 柱状图数值标注的排布角自适应：槽位宽时水平、中等槽位斜排、窄槽位竖排
 *   （标注互不压的验收判据；两档槽位是差别输入）
 *
 * mock 仅外部依赖：datasource 拉取 hook（HTTP 数据源）——本文件全部走静态
 * props.data；离屏可见性 hook 走真实实现。
 */

import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const useDataWidgetMock = vi.hoisted(() => vi.fn())
vi.mock('@/services/schema/dataWidget', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useDataWidget: (...args: unknown[]) => useDataWidgetMock(...args),
}))

import { ChartWidget } from '../ChartWidget'

/** 取数值标注 <text> 节点（内容等于给定文案） */
function labelNode(text: string): SVGTextElement | null {
  const nodes = Array.from(document.querySelectorAll('text'))
  return (nodes.find((n) => n.textContent === text) as SVGTextElement | undefined) ?? null
}

beforeEach(() => {
  useDataWidgetMock.mockReset()
  useDataWidgetMock.mockReturnValue({ data: null, error: null, loading: false })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ChartWidget — 雷达图维度守卫', () => {
  it('两维数据 → 提示至少需要 3 个维度，不渲染畸形雷达图', () => {
    render(
      <ChartWidget
        chartType="radar"
        data={{ labels: ['速度', '稳定'], datasets: [{ data: [3, 4] }] }}
      />,
    )

    expect(screen.getByText('雷达图至少需要3个维度')).toBeInTheDocument()
    expect(document.querySelector('polygon')).toBeNull()
  })

  it('三维数据 → 正常渲染雷达多边形（守卫不误伤合法数据）', () => {
    render(
      <ChartWidget
        chartType="radar"
        data={{ labels: ['速度', '稳定', '成本'], datasets: [{ data: [3, 4, 2] }] }}
      />,
    )

    expect(screen.queryByText('雷达图至少需要3个维度')).not.toBeInTheDocument()
    expect(document.querySelectorAll('polygon').length).toBeGreaterThan(0)
  })
})

describe('ChartWidget — 柱状图数值标注排布角', () => {
  it('中等槽位（11 类别单序列）→ 标注斜排 45°', () => {
    const labels = Array.from({ length: 11 }, (_, i) => `类${i}`)
    render(
      <ChartWidget
        chartType="bar"
        showDataLabels
        data={{ labels, datasets: [{ data: labels.map(() => 1_230_000_000) }] }}
      />,
    )

    const node = labelNode('12.3亿')
    expect(node).not.toBeNull()
    expect(node?.getAttribute('transform')).toContain('rotate(-45')
  })

  it('窄槽位（20 类别单序列）→ 标注竖排 90°', () => {
    const labels = Array.from({ length: 20 }, (_, i) => `类${i}`)
    render(
      <ChartWidget
        chartType="bar"
        showDataLabels
        data={{ labels, datasets: [{ data: labels.map(() => 1_230_000_000) }] }}
      />,
    )

    const node = labelNode('12.3亿')
    expect(node).not.toBeNull()
    expect(node?.getAttribute('transform')).toContain('rotate(-90')
  })
})
