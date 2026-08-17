/**
 * StatusCardWidget 测试（status_card widget，卡片三形态）
 *
 * 核验「插件声明静态数据直通渲染」：
 * - metric 形态：props.metrics 多指标模式渲染 title/value 文字；无 metrics 时单指标兜底显示 "—"
 * - progress 形态（原 ProgressWidget 并入）：value+label 单进度条、steps 多步骤
 * - task 形态（原 TaskCardWidget 并入）：title+status+progress 状态徽标
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusCardWidget } from '../StatusCardWidget'

describe('StatusCardWidget — metric 形态（声明 props 渲染）', () => {
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

describe('StatusCardWidget — progress 形态（原 ProgressWidget 并入）', () => {
  it('label + value 数字推断为进度条，渲染百分比', () => {
    render(<StatusCardWidget label="导入" value={45} />)
    expect(screen.getByText('45%')).toBeInTheDocument()
  })

  it('steps 多步骤模式渲染各步骤标签', () => {
    render(
      <StatusCardWidget
        label="部署"
        steps={[
          { label: '构建', value: 100 },
          { label: '上传', value: 30 },
        ]}
      />,
    )
    expect(screen.getByText('构建')).toBeInTheDocument()
    expect(screen.getByText('上传')).toBeInTheDocument()
    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(screen.getByText('30%')).toBeInTheDocument()
  })
})

describe('StatusCardWidget — task 形态（原 TaskCardWidget 并入）', () => {
  it('progress 数字推断为任务卡，渲染标题/状态徽标/进度', () => {
    render(
      <StatusCardWidget
        title="数据迁移"
        status="running"
        progress={60}
        task_id="t-001"
      />,
    )
    expect(screen.getByText('数据迁移')).toBeInTheDocument()
    expect(screen.getByText('t-001')).toBeInTheDocument()
    expect(screen.getByText('执行中')).toBeInTheDocument()
    expect(screen.getByText('60%')).toBeInTheDocument()
  })

  it('variant 显式指定优先于特征推断', () => {
    render(<StatusCardWidget variant="metric" title="状态" value="正常" progress={60} />)
    expect(screen.getByText('正常')).toBeInTheDocument()
    expect(screen.queryByText('60%')).not.toBeInTheDocument()
  })
})
