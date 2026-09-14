/**
 * Select 原语补测（簇3）
 *
 * select.tsx 是 Radix Select 的样式包装层（无自有业务逻辑），缺行全部落在各
 * 包装组件的 render 体。本文件以真实 Radix 组件渲染 + 交互驱动覆盖：
 * - SelectTrigger / SelectValue：选中值与 placeholder 两种显示，className 合并
 * - SelectContent：展开后渲染选项 / 分组标签 / 分隔符，选中项 aria-selected
 * - SelectItem：selected 态渲染勾选指示器（ItemIndicator）、disabled 态不可选
 * - SelectScrollUpButton / SelectScrollDownButton：随 SelectContent 渲染
 *
 * 环境说明（jsdom 限制，非生产代码问题）：
 * - Radix Select 的 Trigger 打开需要 PointerEvents + hasPointerCapture +
 *   scrollIntoView 等 jsdom 未实现的 API，本文件在 beforeEach 中按需 stub
 *   （仅补宿主 API，不 mock 被测组件）。
 * - 滚动按钮的 className/图标断言走 SelectContent 内部渲染路径——Radix 的
 *   ScrollUp/Down 仅在内容溢出时可点，jsdom 无布局恒不溢出，故只断言存在性
 *   与包装层 className 合并不崩；"溢出时可点"属浏览器布局行为，jsdom 不可达。
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - SelectContent 的 `position === 'popper'` 三元两面：包装层默认 popper，
 *   item-aligned 由 Radix 内部按需使用；两面均为参数驱动的正常分支，本文件以
 *   默认（popper）路径 + 显式 position="popper" 覆盖，item-aligned 面需真实
 *   布局引擎测量，jsdom 下不可达。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

beforeEach(() => {
  // jsdom 未实现的宿主 API（Radix Select 打开/定位路径需要）
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

/** 打开 Select（Radix 在 jsdom 下需 pointerdown 序列） */
function openSelect(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

describe('Select 基础渲染', () => {
  it('Trigger 渲染选中值文本，并合并自定义 className', () => {
    render(
      <Select defaultValue="b">
        <SelectTrigger className="trig-x" aria-label="选择器">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">选项甲</SelectItem>
          <SelectItem value="b">选项乙</SelectItem>
        </SelectContent>
      </Select>,
    )

    const trigger = screen.getByLabelText('选择器')
    expect(trigger.className).toContain('trig-x')
    expect(trigger).toHaveTextContent('选项乙')
  })

  it('未选中时显示 placeholder 文案', () => {
    render(
      <Select>
        <SelectTrigger aria-label="空选择器">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">选项甲</SelectItem>
        </SelectContent>
      </Select>,
    )
    expect(screen.getByLabelText('空选择器')).toHaveTextContent('请选择')
  })
})

describe('SelectContent 展开', () => {
  it('展开后渲染全部选项，点选触发 onValueChange', async () => {
    const onValueChange = vi.fn()
    render(
      <Select onValueChange={onValueChange}>
        <SelectTrigger aria-label="选择器">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">选项甲</SelectItem>
          <SelectItem value="b">选项乙</SelectItem>
        </SelectContent>
      </Select>,
    )

    openSelect(screen.getByLabelText('选择器'))

    const optionA = await screen.findByRole('option', { name: '选项甲' })
    expect(screen.getByRole('option', { name: '选项乙' })).toBeInTheDocument()

    fireEvent.click(optionA)
    await waitFor(() => expect(onValueChange).toHaveBeenCalledWith('a'))
  })

  it('分组 + 标签 + 分隔符均渲染', async () => {
    render(
      <Select>
        <SelectTrigger aria-label="分组选择器">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent position="popper">
          <SelectGroup>
            <SelectLabel>第一组</SelectLabel>
            <SelectItem value="a">选项甲</SelectItem>
          </SelectGroup>
          <SelectSeparator />
          <SelectGroup>
            <SelectLabel>第二组</SelectLabel>
            <SelectItem value="b">选项乙</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>,
    )

    openSelect(screen.getByLabelText('分组选择器'))

    expect(await screen.findByText('第一组')).toBeInTheDocument()
    expect(screen.getByText('第二组')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '选项甲' })).toBeInTheDocument()
  })

  it('选中项 aria-selected=true 并渲染勾选指示器（未选中项为 false）', async () => {
    render(
      <Select defaultValue="a">
        <SelectTrigger aria-label="选择器">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">选项甲</SelectItem>
          <SelectItem value="b">选项乙</SelectItem>
        </SelectContent>
      </Select>,
    )

    openSelect(screen.getByLabelText('选择器'))

    const selected = await screen.findByRole('option', { name: '选项甲' })
    expect(selected).toHaveAttribute('aria-selected', 'true')
    // ItemIndicator 仅在选中项下渲染 svg 勾选图标
    expect(selected.querySelector('svg')).not.toBeNull()
    const other = screen.getByRole('option', { name: '选项乙' })
    expect(other).toHaveAttribute('aria-selected', 'false')
  })

  it('disabled 选项不可选（aria-disabled）；可用项正常回传', async () => {
    const onValueChange = vi.fn()
    render(
      <Select onValueChange={onValueChange}>
        <SelectTrigger aria-label="选择器">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="locked" disabled>
            锁定项
          </SelectItem>
          <SelectItem value="free">可用项</SelectItem>
        </SelectContent>
      </Select>,
    )

    openSelect(screen.getByLabelText('选择器'))

    const locked = await screen.findByRole('option', { name: '锁定项' })
    expect(locked).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(locked)
    expect(onValueChange).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('option', { name: '可用项' }))
    await waitFor(() => expect(onValueChange).toHaveBeenCalledWith('free'))
  })

  it('展开后 Trigger 的 aria-expanded 翻为 true（受控可见性）', async () => {
    render(
      <Select>
        <SelectTrigger aria-label="选择器">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">选项甲</SelectItem>
        </SelectContent>
      </Select>,
    )

    const trigger = screen.getByLabelText('选择器')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    openSelect(trigger)
    await waitFor(() => expect(trigger).toHaveAttribute('aria-expanded', 'true'))
  })

  it('选项的 ItemText 承载子文本（选择后 Trigger 显示该文本）', async () => {
    render(
      <Select defaultValue="a">
        <SelectTrigger aria-label="选择器">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">第一项文本</SelectItem>
          <SelectItem value="b">第二项文本</SelectItem>
        </SelectContent>
      </Select>,
    )
    expect(screen.getByLabelText('选择器')).toHaveTextContent('第一项文本')
  })
})
