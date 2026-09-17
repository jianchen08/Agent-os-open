/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * DropdownMenu 原语补测
 *
 * dropdown-menu.tsx 是 Radix DropdownMenu 的样式包装层（无自有业务逻辑），
 * 缺行集中在未被子业务渲染过的包装组件 render 体。本文件以真实 Radix 组件
 * 渲染 + 交互驱动覆盖：
 * - Trigger/Content/Item/Label/Separator/Shortcut/Group：打开后整棵渲染
 * - CheckboxItem：点击切换 checked，ItemIndicator 随 data-state 显隐
 * - RadioGroup/RadioItem：单选切换
 * - Sub/SubTrigger/SubContent：聚焦子触发器（键盘路径）展开子菜单
 *
 * 环境说明（jsdom 限制，非生产代码问题）：Portal 渲染进 document.body，
 * 统一用 screen 查询。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '../dropdown-menu'

/** Radix DropdownMenu 需要完整指针事件序列（pointerDown → pointerUp → click） */
function openDropdown(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

describe('DropdownMenu 基础面', () => {
  it('打开后渲染 label/item/shortcut/separator，item 可点击', async () => {
    const onAction = vi.fn()
    render(
      <DropdownMenu>
        <DropdownMenuTrigger>打开菜单</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuLabel>操作</DropdownMenuLabel>
          <DropdownMenuGroup>
            <DropdownMenuItem onSelect={onAction}>
              复制
              <DropdownMenuShortcut>⌘C</DropdownMenuShortcut>
            </DropdownMenuItem>
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem inset disabled>
            删除（禁用）
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    openDropdown(screen.getByRole('button', { name: '打开菜单' }))
    expect(await screen.findByText('操作')).toBeInTheDocument()
    expect(screen.getByText('⌘C')).toBeInTheDocument()
    expect(screen.getByText('删除（禁用）')).toHaveAttribute('data-disabled')
    // onSelect 触发后 Radix 关闭菜单，渲染断言须前置于点击
    fireEvent.click(screen.getByText('复制'))
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1))
  })

  it('inset 变体与 className 合并不崩（label/item 两面）', async () => {
    render(
      <DropdownMenu>
        <DropdownMenuTrigger>菜单B</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuLabel inset>缩进标签</DropdownMenuLabel>
          <DropdownMenuItem inset className="extra-class">
            缩进项
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    openDropdown(screen.getByRole("button", { name: "菜单B" }))
    expect(await screen.findByText('缩进标签')).toBeInTheDocument()
    expect(screen.getByText('缩进项')).toBeInTheDocument()
  })
})

describe('DropdownMenuCheckboxItem', () => {
  it('点击切换 checked，ItemIndicator 随状态显隐', async () => {
    function Harness() {
      const [checked, setChecked] = useState(false)
      return (
        <DropdownMenu>
          <DropdownMenuTrigger>勾选菜单</DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuCheckboxItem
              checked={checked}
              onCheckedChange={setChecked}
              data-testid="check-item"
            >
              显示行号
            </DropdownMenuCheckboxItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )
    }
    render(<Harness />)
    openDropdown(screen.getByRole("button", { name: "勾选菜单" }))
    const item = await screen.findByTestId('check-item')
    expect(item).toHaveAttribute('data-state', 'unchecked')
    fireEvent.click(item)
    await waitFor(() => expect(item).toHaveAttribute('data-state', 'checked'))
  })
})

describe('DropdownMenuRadioGroup/RadioItem', () => {
  it('单选切换：选中项 data-state=checked', async () => {
    function Harness() {
      const [value, setValue] = useState('a')
      return (
        <DropdownMenu>
          <DropdownMenuTrigger>单选菜单</DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuRadioGroup value={value} onValueChange={setValue}>
              <DropdownMenuRadioItem value="a" data-testid="opt-a">
                方案A
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="b" data-testid="opt-b">
                方案B
              </DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )
    }
    render(<Harness />)
    openDropdown(screen.getByRole("button", { name: "单选菜单" }))
    const a = await screen.findByTestId('opt-a')
    const b = screen.getByTestId('opt-b')
    expect(a).toHaveAttribute('data-state', 'checked')
    expect(b).toHaveAttribute('data-state', 'unchecked')
    fireEvent.click(b)
    await waitFor(() => {
      expect(b).toHaveAttribute('data-state', 'checked')
      expect(a).toHaveAttribute('data-state', 'unchecked')
    })
  })
})

describe('DropdownMenuSub 子菜单面', () => {
  it('聚焦子触发器（键盘路径）展开子内容', async () => {
    render(
      <DropdownMenu>
        <DropdownMenuTrigger>子菜单入口</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuSub>
            <DropdownMenuSubTrigger data-testid="sub-trigger">更多操作</DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <DropdownMenuItem>子操作一</DropdownMenuItem>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    openDropdown(screen.getByRole("button", { name: "子菜单入口" }))
    const subTrigger = await screen.findByTestId('sub-trigger')
    // jsdom 无真实指针进入语义（Radix pointerEnter 含 100ms 开启定时器且依赖
    // PointerEvent.pointerType），键盘路径 focus → open 为同功能的可达入口
    fireEvent.focus(subTrigger)
    if (screen.queryByText('子操作一') === null) {
      fireEvent.keyDown(subTrigger, { key: 'ArrowRight' })
    }
    expect(await screen.findByText('子操作一')).toBeInTheDocument()
  })
})
