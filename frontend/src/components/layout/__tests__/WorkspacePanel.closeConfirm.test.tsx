// @feature: FP-T12 前端适配 | @ci: frontend-test
// @bug: BUG-79 批量关签确认层 | @ci: frontend-test
/**
 * WorkspacePanel 批量关签确认层测试
 *
 * 契约（BUG-79 实证入口防误触，与 persist 缩容闸 107949d56 互补）：
 * - 右键菜单「关闭其他标签」「关闭所有标签」先出确认层，文案标注影响数量
 *   （「将关闭其余 N 个标签」/「将关闭全部 N 个标签」，固定标签保留如实计数）；
 *   确认后才落到 layoutModeStore 对应动作。
 * - 「关闭本标签」不加确认层（单签可逆，重开成本低）。
 * - 取消 → 不执行任何关签。
 *
 * 测试策略沿用 WorkspacePanelGapsCoverage：真实 layoutModeStore，tabs 由 store
 * 驱动，断言 store 终态。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { StoreDrivenPanel } from './helpers/workspacePanelHarness'
import { makeTab } from './helpers/workspaceTabFactory'


function openTabMenu(tabId: string) {
  fireEvent.contextMenu(screen.getByTestId(`workspace-tab-${tabId}`), {
    clientX: 40,
    clientY: 60,
  })
}

/** 点「确认关闭」并断言关签后剩余的标签 id */
function confirmCloseAndExpectRemaining(ids: string[]): void {
  fireEvent.click(screen.getByRole('button', { name: '确认关闭' }))
  expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(ids)
}

/** 播种 4 签：b 激活、p 固定（固定签不受批量关闭影响，计数必须如实排除） */
function seedTabs() {
  useLayoutModeStore.setState({
    workspaceTabs: [
      makeTab({ id: 'a', title: '标签A' }),
      makeTab({ id: 'b', title: '标签B', isActive: true }),
      makeTab({ id: 'c', title: '标签C' }),
      makeTab({ id: 'p', title: '固定签', isPinned: true }),
    ],
    visitedTabIds: ['a', 'b', 'c', 'p'],
  })
}

beforeEach(() => {
  contributionRegistry.clear()
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
})

describe('WorkspacePanel 批量关签确认层', () => {
  it('「关闭其他标签」先出确认层（标注影响数量、固定签保留），确认后才落关签', () => {
    seedTabs()
    render(<StoreDrivenPanel />)
    openTabMenu('b')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-other'))

    // 确认层出现且未执行关签
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/将关闭其余 2 个标签/)).toBeInTheDocument()
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(4)

    // 确认 → 落关签：目标签与固定签保留
    confirmCloseAndExpectRemaining(['b', 'p'])
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('「关闭所有标签」确认层标注全部影响数量，确认后仅固定签保留', () => {
    seedTabs()
    render(<StoreDrivenPanel />)
    openTabMenu('a')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-all'))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/将关闭全部 3 个标签/)).toBeInTheDocument()
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(4)

    confirmCloseAndExpectRemaining(['p'])
  })

  it('确认层取消：不执行任何关签，菜单状态复位', () => {
    seedTabs()
    render(<StoreDrivenPanel />)
    openTabMenu('b')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-other'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(4)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tab-menu-close-other')).not.toBeInTheDocument()
  })

  it('「关闭本标签」不加确认层，直接落关签', () => {
    seedTabs()
    render(<StoreDrivenPanel />)
    openTabMenu('c')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close'))

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['a', 'b', 'p'])
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('批量关签确认层 — 零影响直执行与 Esc 丢弃', () => {
  /** 全钉住播种：批量关闭影响数为 0（无未固定签可关） */
  function seedAllPinned() {
    useLayoutModeStore.setState({
      workspaceTabs: [
        makeTab({ id: 'p', title: '钉住A', isPinned: true, isActive: true }),
        makeTab({ id: 'q', title: '钉住B', isPinned: true }),
      ],
      visitedTabIds: ['p', 'q'],
    })
  }

  it('影响数为 0（其余全固定）：「关闭其他标签」不加确认层直接执行', () => {
    seedAllPinned()
    render(<StoreDrivenPanel />)
    openTabMenu('p')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-other'))

    // 零影响是 no-op：不出确认层、菜单收起、钉住签保留
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tab-menu-close-other')).not.toBeInTheDocument()
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['p', 'q'])
  })

  it('影响数为 0（全部固定）：「关闭所有标签」不加确认层直接执行', () => {
    seedAllPinned()
    render(<StoreDrivenPanel />)
    openTabMenu('p')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-all'))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['p', 'q'])
  })

  it('确认层 Esc 关闭：丢弃挂起的批量关签，不执行', () => {
    seedTabs()
    render(<StoreDrivenPanel />)
    openTabMenu('b')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-other'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(4)
  })
})
