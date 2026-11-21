/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 对话流标签栏与工作区标签栏的功能对齐（组件级）
 *
 * 契约（对齐目标 = 工作区标签栏的既有能力集，共用实现在 components/layout +
 * hooks，判定单源在 utils/tabCloseSemantics）：
 * - 右键菜单：固定/取消固定、关闭本标签、关闭其他标签、关闭所有标签；
 * - 批量关签先出确认层并如实标注影响数量与保留说明，确认才执行、取消不执行；
 * - 固定签：无单签关闭入口、「关闭本标签」禁用并给出原因、批量关签不波及；
 * - 主对话签（canClose=false）：同样的保护，且不参与固定；
 * - 点外部 / Escape 收起菜单；
 * - tab 带 aria-selected（与工作区标签同一 ARIA 语义）；
 * - 激活签变化时滚入可见区（与工作区标签同一 hook）。
 *
 * 测试策略：真实渲染 AgentTabBar 与共用菜单/确认层，仅断言用户可观察行为
 * （回调参数、渲染文案、可访问性属性、scrollIntoView 调用）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentTabBar, type AgentTab } from '@/components/chat/AgentTabBar'

function makeTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: 'tab-1',
    name: '主管道',
    status: 'running',
    isActive: false,
    canClose: true,
    agentLevel: 2,
    ...overrides,
  }
}

/** 三个签：main（主对话签，不可关）、a（激活）、p（固定） */
function makeTabs(): AgentTab[] {
  return [
    makeTab({ id: 'main', name: '主对话', canClose: false, agentLevel: 1 }),
    makeTab({ id: 'a', name: '子任务甲', isActive: true }),
    makeTab({ id: 'p', name: '固定的子任务', isPinned: true }),
  ]
}

function renderBar(props: Partial<React.ComponentProps<typeof AgentTabBar>> = {}) {
  const handlers = {
    onTabChange: vi.fn(),
    onTabClose: vi.fn(),
    onCloseOtherTabs: vi.fn(),
    onCloseAllTabs: vi.fn(),
    onTogglePin: vi.fn(),
  }
  render(<AgentTabBar tabs={makeTabs()} {...handlers} {...props} />)
  return handlers
}

function openMenu(tabName: string | RegExp) {
  fireEvent.contextMenu(screen.getByRole('tab', { name: tabName }), { clientX: 30, clientY: 40 })
}

describe('AgentTabBar — 右键菜单（与工作区标签栏同一套条目）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('右键打开菜单，四个共用条目齐备', () => {
    renderBar()
    openMenu(/子任务甲/)
    expect(screen.getByTestId('agent-tab-menu-pin')).toBeInTheDocument()
    expect(screen.getByTestId('agent-tab-menu-close')).toBeInTheDocument()
    expect(screen.getByTestId('agent-tab-menu-close-other')).toBeInTheDocument()
    expect(screen.getByTestId('agent-tab-menu-close-all')).toBeInTheDocument()
  })

  it('「关闭本标签」直接关单签（可逆，不拦），收起菜单', () => {
    const h = renderBar()
    openMenu(/子任务甲/)
    fireEvent.click(screen.getByTestId('agent-tab-menu-close'))
    expect(h.onTabClose).toHaveBeenCalledWith('a')
    expect(screen.queryByTestId('agent-tab-menu-close')).toBeNull()
  })

  it('存在可关签时「关闭其他标签」经确认层，文案标注数量与保留说明', () => {
    const h = renderBar({
      tabs: [
        makeTab({ id: 'main', name: '主对话', canClose: false, agentLevel: 1 }),
        makeTab({ id: 'a', name: '子任务甲', isActive: true }),
        makeTab({ id: 'b', name: '子任务乙' }),
        makeTab({ id: 'p', name: '固定的子任务', isPinned: true }),
      ],
    })
    openMenu(/子任务甲/)
    fireEvent.click(screen.getByTestId('agent-tab-menu-close-other'))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/将关闭其余 1 个标签/)).toBeInTheDocument()
    expect(screen.getByText(/固定标签与主对话标签将保留/)).toBeInTheDocument()
    expect(h.onCloseOtherTabs).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确认关闭' }))
    expect(h.onCloseOtherTabs).toHaveBeenCalledWith('a')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('确认层取消 → 不执行任何关签', () => {
    const h = renderBar({
      tabs: [makeTab({ id: 'a', name: '子任务甲' }), makeTab({ id: 'b', name: '子任务乙' })],
    })
    openMenu(/子任务甲/)
    fireEvent.click(screen.getByTestId('agent-tab-menu-close-all'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    expect(h.onCloseAllTabs).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('「关闭所有标签」确认层标注全部影响数量', () => {
    const h = renderBar({
      tabs: [
        makeTab({ id: 'main', name: '主对话', canClose: false, agentLevel: 1 }),
        makeTab({ id: 'a', name: '子任务甲', isActive: true }),
        makeTab({ id: 'b', name: '子任务乙' }),
      ],
    })
    openMenu(/子任务乙/)
    fireEvent.click(screen.getByTestId('agent-tab-menu-close-all'))

    expect(screen.getByText(/将关闭全部 2 个标签/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认关闭' }))
    expect(h.onCloseAllTabs).toHaveBeenCalledTimes(1)
  })

  it('固定签：「关闭本标签」禁用并给出原因，固定条目变「取消固定」', () => {
    renderBar()
    openMenu(/固定的子任务/)
    const closeItem = screen.getByTestId('agent-tab-menu-close')
    expect(closeItem).toBeDisabled()
    expect(closeItem).toHaveAttribute('title', '固定标签不可关闭')
    expect(screen.getByTestId('agent-tab-menu-pin')).toHaveTextContent('取消固定')
  })

  it('主对话签：「关闭本标签」禁用并说明是主标签', () => {
    renderBar()
    openMenu(/主对话/)
    const closeItem = screen.getByTestId('agent-tab-menu-close')
    expect(closeItem).toBeDisabled()
    expect(closeItem).toHaveAttribute('title', '主标签不可关闭')
  })

  it('「固定标签」→ onTogglePin 收到目标签 id', () => {
    const h = renderBar()
    openMenu(/子任务甲/)
    fireEvent.click(screen.getByTestId('agent-tab-menu-pin'))
    expect(h.onTogglePin).toHaveBeenCalledWith('a')
  })

  it('点菜单外部收起菜单', () => {
    renderBar()
    openMenu(/子任务甲/)
    expect(screen.getByTestId('agent-tab-menu-close')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByTestId('agent-tab-menu-close')).toBeNull()
  })

  it('Escape 收起菜单', () => {
    renderBar()
    openMenu(/子任务甲/)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('agent-tab-menu-close')).toBeNull()
  })

  it('未提供批量关签/固定回调 → 不挂菜单（不给死入口）', () => {
    render(
      <AgentTabBar
        tabs={makeTabs()}
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
      />,
    )
    fireEvent.contextMenu(screen.getByRole('tab', { name: /子任务甲/ }), { clientX: 10, clientY: 10 })
    expect(screen.queryByRole('menu')).toBeNull()
  })
})

describe('AgentTabBar — 固定签与 ARIA/滚动对齐', () => {
  it('固定签不渲染单签关闭入口，其余可关签仍渲染', () => {
    renderBar()
    // 可关签 2 个（a、p 中 p 固定 → 只剩一个），主对话签也没有关闭入口
    const closeButtons = screen.getAllByTitle('关闭 Tab')
    expect(closeButtons).toHaveLength(1)

    fireEvent.click(closeButtons[0]!)
    expect(closeButtons[0]).toBeInTheDocument()
  })

  it('tab 带 aria-selected（激活=true），与工作区标签同一 ARIA 语义', () => {
    renderBar()
    expect(screen.getByRole('tab', { name: /主对话/ })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByRole('tab', { name: /子任务甲/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('激活签变化 → 新的激活签滚入可见区（nearest，不牵动纵向）', () => {
    const scrollSpy = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollSpy,
    })

    const { rerender } = render(
      <AgentTabBar
        tabs={[makeTab({ id: 'a', name: '甲', isActive: true }), makeTab({ id: 'b', name: '乙' })]}
        onTabChange={vi.fn()}
      />,
    )
    const tabB = screen.getByRole('tab', { name: /乙/ })
    expect(scrollSpy.mock.contexts).not.toContain(tabB)

    rerender(
      <AgentTabBar
        tabs={[makeTab({ id: 'a', name: '甲' }), makeTab({ id: 'b', name: '乙', isActive: true })]}
        onTabChange={vi.fn()}
      />,
    )
    expect(scrollSpy.mock.contexts).toContain(tabB)
    expect(scrollSpy).toHaveBeenLastCalledWith({ block: 'nearest', inline: 'nearest' })
  })
})
