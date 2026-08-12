/**
 * StatusBadge 测试 —— 状态映射 + 中文文案
 *
 * 意图：统一审查 §3.3 P3「状态文本中英文混排」。StatusBadge 不传 label 时
 * 必须自动查中文表，彻底消除页面直出 active/disabled/deprecated 等英文。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusBadge } from '../StatusBadge'

describe('StatusBadge', () => {
  it('不传 label 时按状态自动渲染中文', () => {
    const { rerender } = render(<StatusBadge status="active" />)
    expect(screen.getByText('已启用')).toBeInTheDocument()
    rerender(<StatusBadge status="disabled" />)
    expect(screen.getByText('已停用')).toBeInTheDocument()
    rerender(<StatusBadge status="deprecated" />)
    expect(screen.getByText('已弃用')).toBeInTheDocument()
  })

  it('传 label 时覆盖中文表（自定义文案优先）', () => {
    render(<StatusBadge status="active" label="在线" />)
    expect(screen.getByText('在线')).toBeInTheDocument()
    expect(screen.queryByText('已启用')).not.toBeInTheDocument()
  })

  it('未知状态回退显示原始值（不崩溃）', () => {
    render(<StatusBadge status="something_new" />)
    expect(screen.getByText('something_new')).toBeInTheDocument()
  })

  it('状态大小写不敏感', () => {
    render(<StatusBadge status="RUNNING" />)
    expect(screen.getByText('运行中')).toBeInTheDocument()
  })
})
