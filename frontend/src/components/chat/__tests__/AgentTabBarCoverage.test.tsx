/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * AgentTabBar / AgentTabItem 覆盖缺口补测
 *
 * 覆盖契约：
 * - AgentTabItem：五种 status 图标（running/completed/waiting_input/failed/unknown），
 *   主 Tab（agentLevel===1）与标题回退（path 连接 → name）、未读数（1..9 与 >9 的
 *   9+ 折叠）、可关闭 Tab 的关闭按钮（stopPropagation，不触发切换）、Enter/Space
 *   键盘激活、isActive 下划线。
 * - AgentTabBar：tab 列表渲染与点击切换、关闭按钮委托、不可关闭 Tab 无关闭按钮、
 *   onNewChat 存在时才渲染新建按钮、纵向滚轮 → 横向滚动（非被动监听 + preventDefault）。
 *
 * 测试策略：真实渲染组件树与真实 useNonPassiveWheel（原生 wheel 事件），仅断言
 * 用户可观察行为（回调参数/渲染文本/滚动位置）。
 *
 * 覆盖补充说明：AgentTabItem 的 switch 兜底（default）在编译期不可达
 * （AgentTabStatus 已五态穷举），但运行期数据（WS/持久化）不受 TS 约束，
 * 已用词表外状态值断言「不崩溃 + 回退默认图标」的可观察契约。
 *
 * 不可达说明：AgentTabBar 第 50 行 `if (!el) return`（wheel 回调内空 ref 保护）
 * 为分支计数未满（行本身已覆盖）——useNonPassiveWheel 仅在元素挂载时绑定监听，
 * 回调触发时 scrollContainerRef 必然已赋值，属防御性兜底。
 */

import { createEvent, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AgentTabBar, type AgentTab } from '@/components/chat/AgentTabBar'
import { AgentTabItem } from '@/components/chat/AgentTabItem'
import { makeDataTransfer } from '@/test/dndTestUtils'
import type { AgentTabItemData } from '@/components/chat/AgentTabItem'

function makeItem(overrides: Partial<AgentTabItemData> = {}): AgentTabItemData {
  return {
    id: 'tab-1',
    name: '主管道',
    agentLevel: 2,
    status: 'running',
    isActive: false,
    canClose: true,
    ...overrides,
  }
}

function makeTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: 'tab-1',
    name: '主管道',
    status: 'running',
    isActive: true,
    canClose: true,
    agentLevel: 1,
    ...overrides,
  }
}

describe('AgentTabItem — 状态图标与交互', () => {
  it.each([
    ['running', '\u25CF'],
    ['completed', '\u2713'],
    ['waiting_input', '\uD83D\uDCAC'],
    ['failed', '\u2715'],
    ['unknown', '\uFF1F'],
  ] as const)('status=%s 渲染对应状态图标 %s', (status, icon) => {
    render(<AgentTabItem tab={makeItem({ status })} onClick={() => {}} />)
    const tab = screen.getByRole('tab')
    expect(tab.textContent).toContain(icon)
  })

  it('运行期出现词表外状态时回退默认图标 ●（防御分支，不崩溃）', () => {
    // status 的 TS 联合已封闭；运行期数据（WS/持久化）不受编译期约束，
    // 故按「未知状态不崩溃且可辨认」的契约断言兜底渲染
    render(
      <AgentTabItem
        tab={makeItem({ status: 'not_a_known_status' as never })}
        onClick={() => {}}
      />,
    )
    expect(screen.getByRole('tab').textContent).toContain('●')
  })

  it('主 Tab 与子 Tab 的标题回退：有 path 用箭头连接，无 path 用 name', () => {
    const { rerender } = render(
      <AgentTabItem
        tab={makeItem({ name: '子任务', path: ['主管道', '子任务'], agentLevel: 2 })}
        onClick={() => {}}
      />,
    )
    expect(screen.getByRole('tab')).toHaveAttribute('title', '主管道 \u2192 子任务')

    rerender(<AgentTabItem tab={makeItem({ name: '独立会话', agentLevel: 3 })} onClick={() => {}} />)
    expect(screen.getByRole('tab')).toHaveAttribute('title', '独立会话')
  })

  it.each([
    [3, '3'],
    [9, '9'],
    [12, '9+'],
  ])('未读数 %i 渲染为 %s', (unreadCount, label) => {
    render(<AgentTabItem tab={makeItem({ unreadCount })} onClick={() => {}} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('unreadCount 缺省时不渲染未读徽标（tab 文本仅图标 + 名称）', () => {
    render(
      <AgentTabItem tab={makeItem({ name: '无未读', status: 'completed' })} onClick={() => {}} />,
    )
    expect(screen.getByRole('tab').textContent).toBe('\u2713无未读')
  })

  // 现状契约（疑似缺陷，已在回报中记录不修改生产代码）：unreadCount===0 时
  // JSX `{tab.unreadCount && ...}` 求值为 0 并作为文本节点渲染出字面 "0"。
  it('unreadCount=0 时渲染字面 0（数字短路直出，非徽标元素）', () => {
    render(
      <AgentTabItem tab={makeItem({ name: '无未读', status: 'completed', unreadCount: 0 })} onClick={() => {}} />,
    )
    const tab = screen.getByRole('tab')
    expect(tab.textContent).toBe('\u2713无未读0')
    expect(tab.querySelector('.rounded-full')).toBeNull()
  })

  it('点击关闭按钮只触发 onClose，不冒泡触发 onClick', () => {
    const onClick = vi.fn()
    const onClose = vi.fn()
    render(<AgentTabItem tab={makeItem()} onClick={onClick} onClose={onClose} />)
    fireEvent.click(screen.getByTitle('关闭 Tab'))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('canClose=true 但未提供 onClose 时不渲染关闭按钮', () => {
    render(<AgentTabItem tab={makeItem({ canClose: true })} onClick={() => {}} />)
    expect(screen.queryByTitle('关闭 Tab')).toBeNull()
  })

  it.each([['Enter'], [' ']])('键盘 %s 触发 onClick', (key) => {
    const onClick = vi.fn()
    render(<AgentTabItem tab={makeItem()} onClick={onClick} />)
    fireEvent.keyDown(screen.getByRole('tab'), { key })
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('其他按键不触发 onClick', () => {
    const onClick = vi.fn()
    render(<AgentTabItem tab={makeItem()} onClick={onClick} />)
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'a' })
    expect(onClick).not.toHaveBeenCalled()
  })

  it('自定义 className 应用到 tab 根节点', () => {
    render(<AgentTabItem tab={makeItem()} onClick={() => {}} className="my-custom-tab" />)
    expect(screen.getByRole('tab')).toHaveClass('my-custom-tab')
  })
})

describe('AgentTabBar — tab 列表与滚轮', () => {
  it('渲染全部 tab，点击切换回调收到对应 id', () => {
    const onTabChange = vi.fn()
    render(
      <AgentTabBar
        tabs={[
          makeTab({ id: 'a', name: '主管道' }),
          makeTab({ id: 'b', name: '子任务', agentLevel: 2, isActive: false }),
        ]}
        onTabChange={onTabChange}
      />,
    )
    expect(screen.getAllByRole('tab')).toHaveLength(2)
    fireEvent.click(screen.getByRole('tab', { name: /子任务/ }))
    expect(onTabChange).toHaveBeenCalledWith('b')
  })

  it('关闭按钮委托 onTabClose；canClose=false 的 tab 无关闭入口', () => {
    const onTabClose = vi.fn()
    render(
      <AgentTabBar
        tabs={[
          makeTab({ id: 'a', name: '主管道', canClose: false }),
          makeTab({ id: 'b', name: '子任务', agentLevel: 2, isActive: false, canClose: true }),
        ]}
        onTabChange={() => {}}
        onTabClose={onTabClose}
      />,
    )
    expect(screen.getAllByTitle('关闭 Tab')).toHaveLength(1)
    fireEvent.click(screen.getByTitle('关闭 Tab'))
    expect(onTabClose).toHaveBeenCalledWith('b')
  })

  it('未提供 onTabClose 时关闭按钮仍渲染但不抛错', () => {
    render(
      <AgentTabBar tabs={[makeTab({ id: 'a', canClose: true })]} onTabChange={() => {}} />,
    )
    expect(() => fireEvent.click(screen.getByTitle('关闭 Tab'))).not.toThrow()
  })

  it('有 onNewChat 才渲染新建对话按钮并触发回调', () => {
    const onNewChat = vi.fn()
    const { rerender } = render(
      <AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} />,
    )
    expect(screen.queryByTitle('新建对话')).toBeNull()

    rerender(<AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} onNewChat={onNewChat} />)
    fireEvent.click(screen.getByTitle('新建对话'))
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('纵向滚轮把手势转成横向滚动（非被动监听，preventDefault 生效）', () => {
    render(<AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} />)
    const tablist = screen.getByRole('tablist')
    // jsdom 不实现布局，scrollLeft 手动可写
    tablist.scrollLeft = 0
    const event = new WheelEvent('wheel', { deltaY: 120, deltaX: 0, cancelable: true, bubbles: true })
    tablist.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(tablist.scrollLeft).toBe(120)
  })

  it('横向为主的滚轮不拦截，保持原生横向滚动', () => {
    render(<AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} />)
    const tablist = screen.getByRole('tablist')
    tablist.scrollLeft = 10
    const event = new WheelEvent('wheel', { deltaY: 5, deltaX: 40, cancelable: true, bubbles: true })
    tablist.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(tablist.scrollLeft).toBe(10)
  })
})

describe('AgentTabItem — 拖拽换位', () => {
  /** 同一路径组（path 全等）的两个 tab（t1/t2）；extras 追加异组 tab */
  function renderSamePathPair(extra?: AgentTabItemData[]) {
    const onReorder = vi.fn()
    const tabs = [
      makeItem({ id: 't1', name: '子任务一', path: ['主管道'] }),
      makeItem({ id: 't2', name: '子任务二', path: ['主管道'] }),
      ...(extra ?? []),
    ]
    render(
      <>
        {tabs.map((tab) => (
          <AgentTabItem key={tab.id} tab={tab} onClick={() => {}} onReorder={onReorder} />
        ))}
      </>,
    )
    return onReorder
  }

  it('dragStart 写入拖拽载荷（effectAllowed=move、text/plain=tab id）', () => {
    renderSamePathPair()
    const dt = makeDataTransfer()

    fireEvent.dragStart(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })

    expect(dt.effectAllowed).toBe('move')
    expect(dt.getData('text/plain')).toBe('t1')

    // 自 drop 复位模块态拖拽源，不外溢到后续用例
    fireEvent.drop(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })
  })

  it('dragOver 同组其他 tab 拦截默认行为；自身与异组不拦截', () => {
    renderSamePathPair([makeItem({ id: 't3', name: '别组', path: ['主管道', '别组'] })])
    const dt = makeDataTransfer()
    fireEvent.dragStart(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })

    const overSelf = createEvent.dragOver(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })
    fireEvent(screen.getByRole('tab', { name: /子任务一/ }), overSelf)
    expect(overSelf.defaultPrevented).toBe(false)

    const overSameGroup = createEvent.dragOver(screen.getByRole('tab', { name: /子任务二/ }), { dataTransfer: dt })
    fireEvent(screen.getByRole('tab', { name: /子任务二/ }), overSameGroup)
    expect(overSameGroup.defaultPrevented).toBe(true)

    const overOtherGroup = createEvent.dragOver(screen.getByRole('tab', { name: /别组/ }), { dataTransfer: dt })
    fireEvent(screen.getByRole('tab', { name: /别组/ }), overOtherGroup)
    expect(overOtherGroup.defaultPrevented).toBe(false)

    // drop 异组不换位，仅复位拖拽源
    fireEvent.drop(screen.getByRole('tab', { name: /别组/ }), { dataTransfer: dt })
  })

  it('drop 同组其他 tab：onReorder 收到（拖拽源 id, 目标 id）', () => {
    const onReorder = renderSamePathPair()
    const dt = makeDataTransfer()
    fireEvent.dragStart(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })

    fireEvent.drop(screen.getByRole('tab', { name: /子任务二/ }), { dataTransfer: dt })

    expect(onReorder).toHaveBeenCalledTimes(1)
    expect(onReorder).toHaveBeenCalledWith('t1', 't2')
  })

  it('drop 守卫：无拖拽源 / 自身 / 异组均不回调', () => {
    const onReorder = renderSamePathPair([makeItem({ id: 't3', name: '别组', path: ['主管道', '别组'] })])
    const dt = makeDataTransfer()

    // 未经 dragStart：无拖拽源
    fireEvent.drop(screen.getByRole('tab', { name: /子任务二/ }), { dataTransfer: makeDataTransfer() })

    // drop 到自身
    fireEvent.dragStart(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })
    fireEvent.drop(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })

    // drop 到异组
    fireEvent.dragStart(screen.getByRole('tab', { name: /子任务一/ }), { dataTransfer: dt })
    fireEvent.drop(screen.getByRole('tab', { name: /别组/ }), { dataTransfer: dt })

    expect(onReorder).not.toHaveBeenCalled()
  })

  it('无 path 的 tab 以空串归组：同组可拖拽换位', () => {
    const onReorder = vi.fn()
    render(
      <>
        <AgentTabItem tab={makeItem({ id: 't1', path: undefined })} onClick={() => {}} onReorder={onReorder} />
        <AgentTabItem tab={makeItem({ id: 't2', path: undefined })} onClick={() => {}} onReorder={onReorder} />
      </>,
    )
    const dt = makeDataTransfer()
    const first = screen.getAllByRole('tab')[0]!
    const second = screen.getAllByRole('tab')[1]!

    fireEvent.dragStart(first, { dataTransfer: dt })
    const over = createEvent.dragOver(second, { dataTransfer: dt })
    fireEvent(second, over)
    expect(over.defaultPrevented).toBe(true)

    fireEvent.drop(second, { dataTransfer: dt })
    expect(onReorder).toHaveBeenCalledWith('t1', 't2')
  })
})
