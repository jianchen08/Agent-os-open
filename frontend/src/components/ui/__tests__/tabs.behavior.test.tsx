/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 基础 Tabs 组件行为补测：
 * - 非受控受控桥：未传 value 时点击触发器由组件自身状态切换（defaultValue 起步）
 * - 受控模式：传 value 时点击不改内部态，由 onValueChange 交由宿主裁决
 * - 触发器脱离 Tabs 上下文 → 显式报错（不静默渲染）
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../tabs'

function renderTabs(props: { value?: string; defaultValue?: string; onValueChange?: (v: string) => void }) {
  return render(
    <Tabs {...props}>
      <TabsList>
        <TabsTrigger value="a">甲</TabsTrigger>
        <TabsTrigger value="b">乙</TabsTrigger>
      </TabsList>
      <TabsContent value="a">甲内容</TabsContent>
      <TabsContent value="b">乙内容</TabsContent>
    </Tabs>,
  )
}

describe('Tabs — 非受控与受控', () => {
  it('非受控（defaultValue）→ 点击触发器由内部状态切换选中与内容', () => {
    renderTabs({ defaultValue: 'a' })
    expect(screen.getByText('甲内容')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: '乙' }))

    expect(screen.getByRole('tab', { name: '乙' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('乙内容')).toBeInTheDocument()
    expect(screen.queryByText('甲内容')).not.toBeInTheDocument()
  })

  it('受控（value）→ 点击只上报，不改内部态；宿主更新后跟随', () => {
    const onValueChange = vi.fn()
    const { rerender } = renderTabs({ value: 'a', onValueChange })

    fireEvent.click(screen.getByRole('tab', { name: '乙' }))
    expect(onValueChange).toHaveBeenCalledWith('b')
    // 宿主未更新值 → 视图保持在甲
    expect(screen.getByText('甲内容')).toBeInTheDocument()

    rerender(
      <Tabs value="b">
        <TabsList>
          <TabsTrigger value="a">甲</TabsTrigger>
          <TabsTrigger value="b">乙</TabsTrigger>
        </TabsList>
        <TabsContent value="a">甲内容</TabsContent>
        <TabsContent value="b">乙内容</TabsContent>
      </Tabs>,
    )
    expect(screen.getByText('乙内容')).toBeInTheDocument()
  })

  it('触发器脱离 Tabs 上下文 → 显式报错（错误使用不被静默吞掉）', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<TabsTrigger value="x">孤立</TabsTrigger>)).toThrow(
      'Tabs 组件必须在 Tabs 内部使用',
    )
    consoleError.mockRestore()
  })
})
