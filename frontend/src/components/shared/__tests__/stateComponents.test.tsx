/**
 * 轻量展示组件测试：EmptyState / ErrorState / Pagination
 *
 * 纯 props→渲染，无外部依赖。
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { EmptyState } from '@/components/shared/EmptyState'
import { ErrorState } from '@/components/shared/ErrorState'
import { Pagination } from '@/components/shared/Pagination'
import type { LucideIcon } from '@/assets/icons'

const FakeIcon = (() => null) as unknown as LucideIcon

describe('EmptyState', () => {
  it('渲染标题与图标；无 description/action 时不渲染对应区域', () => {
    const { container } = render(<EmptyState icon={FakeIcon} title="空空如也" />)
    expect(screen.getByText('空空如也')).toBeInTheDocument()
    expect(container.querySelector('p.text-muted-foreground\\/60')).toBeNull()
  })

  it('渲染 description 与 action 区域', () => {
    render(
      <EmptyState
        icon={FakeIcon}
        title="无数据"
        description="暂无可展示内容"
        action={<button>新建</button>}
      />,
    )
    expect(screen.getByText('暂无可展示内容')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建' })).toBeInTheDocument()
  })
})

describe('ErrorState', () => {
  it('默认 inline 变体：横幅 + 无重试按钮', () => {
    const { container } = render(<ErrorState message="加载失败" />)
    expect(screen.getByText('加载失败')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull()
    expect(container.querySelector('div')).not.toBeNull()
  })

  it('inline 变体带 onRetry → 显示重试按钮，点击触发回调', () => {
    const onRetry = vi.fn()
    render(<ErrorState message="出错了" onRetry={onRetry} />)
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('center 变体：居中展示 + 重试按钮触发回调', () => {
    const onRetry = vi.fn()
    render(<ErrorState message="页面崩溃" variant="center" onRetry={onRetry} />)
    expect(screen.getByText('页面崩溃')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('center 变体无 onRetry → 不显示重试按钮', () => {
    render(<ErrorState message="仅提示" variant="center" />)
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull()
  })
})

describe('Pagination', () => {
  it('显示 current/totalPages；中间页两按钮均可用', () => {
    const onChange = vi.fn()
    render(<Pagination current={2} total={50} pageSize={10} onChange={onChange} />)
    expect(screen.getByText('2 / 5')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '上一页' }))
    expect(onChange).toHaveBeenCalledWith(1)
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(onChange).toHaveBeenCalledWith(3)
  })

  it('首页时上一页禁用；末页时下一页禁用', () => {
    const onChange = vi.fn()
    const { rerender } = render(<Pagination current={1} total={100} onChange={onChange} />)
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(onChange).toHaveBeenCalledWith(2)

    rerender(<Pagination current={5} total={100} onChange={onChange} />)
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
  })

  it('禁用态点击不触发 onChange', () => {
    const onChange = vi.fn()
    render(<Pagination current={1} total={10} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '上一页' }))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('total 为 0 → totalPages 兜底为 1，仅显示 1 / 1', () => {
    const onChange = vi.fn()
    render(<Pagination current={1} total={0} onChange={onChange} />)
    expect(screen.getByText('1 / 1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
  })

  it('非整除 total → 向上取整（total=5, pageSize=2 → 3 页）', () => {
    render(<Pagination current={2} total={5} pageSize={2} onChange={vi.fn()} />)
    expect(screen.getByText('2 / 3')).toBeInTheDocument()
  })
})
