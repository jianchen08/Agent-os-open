/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WorkspacePanel 覆盖缺口补测（与 WorkspacePanel.test.tsx 互补，不重复）
 *
 * 覆盖契约：
 * - 空态「打开任务管理」按钮经 workspacePanelOpener 打开 /tasks
 * - 标签右键菜单：关闭本标签 / 关闭其他标签 / 关闭所有标签 三条动作落到
 *   layoutModeStore 的对应动作，DOM 随 store 更新（真实消费方 FiveSpaceLayout
 *   即从 store 取 tabs，此处用等价订阅包装组件还原该链路）
 * - 固定标签的「关闭本标签」禁用且无标签级 × 入口
 * - 菜单点击外部 / Escape 关闭
 * - 全屏按钮：isFullscreen 切换图标与无障碍名，点击触发 onFullscreen；
 *   未提供 onFullscreen 时不渲染按钮
 * - 纵向滚轮转横向滚动（非被动监听）
 * - 懒挂载：仅激活或 visited 的 Tab 渲染内容，其余占位
 *
 * 测试策略：真实 layoutModeStore / ContributionRegistry / opener 打开链路
 * （真实依赖，tabs/visited 由 store 驱动，点击落点断言 store 终态）。
 */

import { createEvent, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { makeDataTransfer } from '@/test/dndTestUtils'
import { StoreDrivenPanel } from './helpers/workspacePanelHarness'
import { makeTab } from './helpers/workspaceTabFactory'

/** 三个普通标签的标准播种（a 激活、b/c 未激活），visited 可指定 */
function seedThreeTabs(visitedTabIds: string[] = []): void {
  useLayoutModeStore.setState({
    workspaceTabs: [
      makeTab({ id: 'a', title: '甲', isActive: true }),
      makeTab({ id: 'b', title: '乙', isActive: false }),
      makeTab({ id: 'c', title: '丙', isActive: false }),
    ],
    visitedTabIds,
  })
}

function openTabMenu(tabId: string) {
  fireEvent.contextMenu(screen.getByTestId(`workspace-tab-${tabId}`), {
    clientX: 40,
    clientY: 60,
  })
}

describe('WorkspacePanel — 空态与全屏', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    contributionRegistry.clear()
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('空态渲染导航页，点击声明条目打开对应工作区页签（真实 opener 链路）', () => {
    contributionRegistry.register({
      type: 'pages', id: 'tasks', title: '任务管理',
      space: 'workspace', slot: 'tab', path: '/tasks', pluginId: 'task_service',
    })
    render(<StoreDrivenPanel />)
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('nav-item-task_service:tasks'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-tasks'])
    expect(tabs[0]?.isActive).toBe(true)
  })

  it('全屏按钮随 isFullscreen 切换语义，点击回调触发', () => {
    const onFullscreen = vi.fn()
    useLayoutModeStore.setState({ workspaceTabs: [makeTab()], visitedTabIds: [] })
    const { rerender } = render(
      <StoreDrivenPanel onFullscreen={onFullscreen} isFullscreen={false} />,
    )
    fireEvent.click(screen.getByTestId('workspace-toggle-fullscreen'))
    expect(onFullscreen).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('铺满全屏')).toBeInTheDocument()

    rerender(<StoreDrivenPanel onFullscreen={onFullscreen} isFullscreen />)
    expect(screen.getByLabelText('退出全屏')).toBeInTheDocument()
  })

  it('未提供 onFullscreen 时不渲染全屏按钮', () => {
    useLayoutModeStore.setState({ workspaceTabs: [makeTab()], visitedTabIds: [] })
    render(<StoreDrivenPanel />)
    expect(screen.queryByTestId('workspace-toggle-fullscreen')).toBeNull()
  })
})

describe('WorkspacePanel — 右键菜单动作', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedThreeTabs()
  })

  it('「关闭本标签」只移除目标标签', () => {
    render(<StoreDrivenPanel />)
    openTabMenu('b')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close'))

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['a', 'c'])
    expect(screen.queryByTestId('workspace-tab-b')).toBeNull()
    expect(screen.getByTestId('workspace-tab-a')).toBeInTheDocument()
  })

  it('「关闭其他标签」经确认层后保留目标标签（BUG-79 防误触）', () => {
    render(<StoreDrivenPanel />)
    openTabMenu('b')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-other'))
    // 批量关签先出确认层，确认后才落关签
    fireEvent.click(screen.getByRole('button', { name: '确认关闭' }))
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['b'])
    expect(screen.queryByTestId('workspace-tab-a')).toBeNull()
  })

  it('「关闭所有标签」经确认层后清空列表并回到空态（BUG-79 防误触）', () => {
    render(<StoreDrivenPanel />)
    openTabMenu('a')
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-all'))
    fireEvent.click(screen.getByRole('button', { name: '确认关闭' }))
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()
  })

  it('固定标签的「关闭本标签」禁用', () => {
    useLayoutModeStore.setState({
      workspaceTabs: [makeTab({ id: 'p', title: '钉住', isPinned: true })],
      visitedTabIds: [],
    })
    render(<StoreDrivenPanel />)
    openTabMenu('p')
    const closeItem = screen.getByTestId('workspace-tab-menu-close')
    expect(closeItem).toBeDisabled()
    expect(closeItem).toHaveAttribute('title', '固定标签不可关闭')
    expect(screen.queryByTestId('workspace-tab-close-p')).toBeNull()
  })

  it('点击菜单外部关闭菜单', () => {
    render(<StoreDrivenPanel />)
    openTabMenu('a')
    expect(screen.getByTestId('workspace-tab-menu-close')).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    expect(screen.queryByTestId('workspace-tab-menu-close')).toBeNull()
  })

  it('Escape 关闭菜单', () => {
    render(<StoreDrivenPanel />)
    openTabMenu('a')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('workspace-tab-menu-close')).toBeNull()
  })
})

describe('WorkspacePanel — 懒挂载与滚轮', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('仅激活与 visited 的 Tab 渲染内容，其余为 aria-hidden 占位', () => {
    seedThreeTabs(['c'])
    render(<StoreDrivenPanel />)

    expect(screen.getByText('内容-a')).toBeInTheDocument()
    expect(screen.getByText('内容-c')).toBeInTheDocument()
    expect(screen.queryByText('内容-b')).toBeNull()
  })

  it('纵向滚轮转横向滚动', () => {
    useLayoutModeStore.setState({ workspaceTabs: [makeTab()], visitedTabIds: [] })
    render(<StoreDrivenPanel />)
    const tablist = screen.getByRole('tablist')
    tablist.scrollLeft = 0
    const event = new WheelEvent('wheel', { deltaY: 90, deltaX: 0, cancelable: true, bubbles: true })
    tablist.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(tablist.scrollLeft).toBe(90)
  })

  it('横向为主的滚轮不拦截', () => {
    useLayoutModeStore.setState({ workspaceTabs: [makeTab()], visitedTabIds: [] })
    render(<StoreDrivenPanel />)
    const tablist = screen.getByRole('tablist')
    tablist.scrollLeft = 5
    const event = new WheelEvent('wheel', { deltaY: 2, deltaX: 30, cancelable: true, bubbles: true })
    tablist.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(tablist.scrollLeft).toBe(5)
  })
})

describe('WorkspacePanel — 标签拖拽换位', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedThreeTabs()
  })

  it('dragStart 记录拖拽源并写入 dataTransfer 载荷（effectAllowed=move、text/plain=tab id）', () => {
    render(<StoreDrivenPanel />)
    const dt = makeDataTransfer()

    fireEvent.dragStart(screen.getByTestId('workspace-tab-a'), { dataTransfer: dt })

    expect(dt.effectAllowed).toBe('move')
    expect(dt.getData('text/plain')).toBe('a')
  })

  it('dragOver 其他标签拦截默认行为（可成为放置目标），拖过自身不拦截', () => {
    render(<StoreDrivenPanel />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(screen.getByTestId('workspace-tab-a'), { dataTransfer: dt })

    const overSelf = createEvent.dragOver(screen.getByTestId('workspace-tab-a'), { dataTransfer: dt })
    fireEvent(screen.getByTestId('workspace-tab-a'), overSelf)
    expect(overSelf.defaultPrevented).toBe(false)

    const overOther = createEvent.dragOver(screen.getByTestId('workspace-tab-b'), { dataTransfer: dt })
    fireEvent(screen.getByTestId('workspace-tab-b'), overOther)
    expect(overOther.defaultPrevented).toBe(true)
  })

  it('drop 到其他标签：落 store 的 reorderWorkspaceTabs 换位', () => {
    render(<StoreDrivenPanel />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(screen.getByTestId('workspace-tab-a'), { dataTransfer: dt })

    fireEvent.drop(screen.getByTestId('workspace-tab-b'), { dataTransfer: dt })

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['b', 'a', 'c'])
  })

  it('drop 兜底：拖拽源为空时从 dataTransfer 读回（跨窗口拖拽形态）', () => {
    render(<StoreDrivenPanel />)

    fireEvent.drop(screen.getByTestId('workspace-tab-c'), {
      dataTransfer: makeDataTransfer({ 'text/plain': 'a' }),
    })

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['b', 'c', 'a'])
  })

  it('drop 到自身与无拖拽源：不换位', () => {
    render(<StoreDrivenPanel />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(screen.getByTestId('workspace-tab-a'), { dataTransfer: dt })

    fireEvent.drop(screen.getByTestId('workspace-tab-a'), { dataTransfer: dt })
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['a', 'b', 'c'])

    // dragStart 已被上次 drop 消费复位，且 dataTransfer 无载荷
    fireEvent.drop(screen.getByTestId('workspace-tab-b'), { dataTransfer: makeDataTransfer() })
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual(['a', 'b', 'c'])
  })
})
