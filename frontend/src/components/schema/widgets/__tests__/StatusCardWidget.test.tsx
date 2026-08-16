/**
 * StatusCardWidget 测试（status_card widget）
 *
 * 核验「插件声明静态数据直通渲染」：props.metrics 多指标模式渲染
 * title/value 文字；无 metrics 时单指标兜底显示 "—"。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusCardWidget } from '../StatusCardWidget'

describe('StatusCardWidget — 声明 props 渲染', () => {
  it('metrics 多指标模式渲染 title/value 文字', () => {
    render(
      <StatusCardWidget
        title="DSH 状态"
        metrics={[
          { title: '插件', value: '3' },
          { title: '运行中工具', value: '2' },
        ]}
      />,
    )
    expect(screen.getByText('插件')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('运行中工具')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.queryByText('—')).not.toBeInTheDocument()
  })

  it('无 metrics 单指标模式：有 value 渲染值', () => {
    render(<StatusCardWidget title="状态" value="正常" />)
    expect(screen.getByText('状态')).toBeInTheDocument()
    expect(screen.getByText('正常')).toBeInTheDocument()
  })

  it('无 metrics 且无 value 时兜底渲染 "—"（原占位行为保留）', () => {
    render(<StatusCardWidget title="状态" />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
