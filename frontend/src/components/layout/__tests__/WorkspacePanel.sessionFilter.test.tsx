/** @feature FP-T12 前端适配（工作区页签按归属会话过滤） @vision V6 可即用 @ci frontend-test */
/**
 * WorkspacePanel「只看当前会话」过滤测试
 *
 * 契约：会话派生页签（ownerSessionId）默认只在其归属会话为当前会话时可见，
 * 全局页签（无 ownerSessionId）任何会话下恒可见；过滤只动视图层——隐藏≠关闭，
 * store 的 workspaceTabs 集合不因过滤变化；激活签被隐藏时自动回退切换到第一个
 * 可见签（沿用 onTabChange/setActiveTab 通道）；activeSessionId 为 null 时开档下
 * 会话派生签全部隐藏（严格原则）。
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import { useSessionStore } from '@/stores/sessionStore'
import { makeTab } from './helpers/workspaceTabFactory'
import type { WorkspaceTab } from '@/types/layout'

/** 渲染面板（tabs 注入；onTabChange 默认 spy 供回退切换断言） */
function renderPanel(tabs: Array<Partial<WorkspaceTab> & { id: string }>, onTabChange?: (tabId: string) => void) {
  render(
    <WorkspacePanel
      tabs={tabs.map((t) => makeTab(t))}
      onTabChange={onTabChange ?? (() => {})}
      onTabClose={() => {}}
      renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
    />,
  )
}

describe('WorkspacePanel — 只看当前会话过滤', () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: null })
  })

  it('默认开：owner 签按当前会话显隐，全局签恒在', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    renderPanel([
      { id: 'g', title: '全局签', isActive: true },
      { id: 'mine', title: '本会话签', ownerSessionId: 'sess-1' },
      { id: 'other', title: '别会话签', ownerSessionId: 'sess-2' },
    ])
    expect(screen.getByTestId('workspace-tab-g')).toBeInTheDocument()
    expect(screen.getByTestId('workspace-tab-mine')).toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tab-other')).not.toBeInTheDocument()
  })

  it('关：全部可见（隐藏的标签原样回来），开关状态可逆', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    renderPanel([
      { id: 'g', title: '全局签', isActive: true },
      { id: 'other', title: '别会话签', ownerSessionId: 'sess-2' },
    ])
    expect(screen.queryByTestId('workspace-tab-other')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('workspace-session-filter-toggle'))
    expect(screen.getByTestId('workspace-tab-other')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('workspace-session-filter-toggle'))
    expect(screen.queryByTestId('workspace-tab-other')).not.toBeInTheDocument()
  })

  it('activeSessionId 为 null：开档下会话派生签全部隐藏，全局签恒在', () => {
    renderPanel([
      { id: 'g', title: '全局签', isActive: true },
      { id: 'mine', title: '本会话签', ownerSessionId: 'sess-1' },
    ])
    expect(screen.getByTestId('workspace-tab-g')).toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tab-mine')).not.toBeInTheDocument()
  })

  it('激活签被隐藏 → 自动切换到第一个可见签（沿用 onTabChange 通道）', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    const onTabChange = vi.fn()
    renderPanel(
      [
        { id: 'hidden-active', title: '别会话激活签', ownerSessionId: 'sess-2', isActive: true },
        { id: 'g', title: '全局签', isActive: false },
      ],
      onTabChange,
    )
    expect(onTabChange).toHaveBeenCalledWith('g')
  })

  it('可见签中有激活签时不触发回退切换', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    const onTabChange = vi.fn()
    renderPanel(
      [
        { id: 'mine', title: '本会话激活签', ownerSessionId: 'sess-1', isActive: true },
        { id: 'g', title: '全局签' },
      ],
      onTabChange,
    )
    expect(onTabChange).not.toHaveBeenCalled()
  })

  it('开关 tooltip 提示被隐藏标签数', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    renderPanel([
      { id: 'g', title: '全局签', isActive: true },
      { id: 'o1', title: '别会话签1', ownerSessionId: 'sess-2' },
      { id: 'o2', title: '别会话签2', ownerSessionId: 'sess-3' },
    ])
    expect(screen.getByTestId('workspace-session-filter-toggle')).toHaveAttribute(
      'title',
      '已按当前会话隐藏 2 个标签（点击显示全部）',
    )
  })

  it('无隐藏标签时 tooltip 不报数量；关闭开关后 title 提示全量语义', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    renderPanel([
      { id: 'g', title: '全局签', isActive: true },
      { id: 'mine', title: '本会话签', ownerSessionId: 'sess-1' },
    ])
    const toggle = screen.getByTestId('workspace-session-filter-toggle')
    expect(toggle).toHaveAttribute('title', '只看当前会话（当前没有被隐藏的标签）')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('title', '显示全部标签（开启后按当前会话过滤）')
  })

  it('内容区同步过滤：被隐藏页签不渲染内容；全部隐藏时落导航页兜底', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    renderPanel([
      { id: 'mine', title: '本会话签', ownerSessionId: 'sess-1', isActive: true },
      { id: 'other', title: '别会话签', ownerSessionId: 'sess-2' },
    ])
    expect(screen.getByText('内容-mine')).toBeInTheDocument()
    expect(screen.queryByText('内容-other')).not.toBeInTheDocument()

    // 切到无归属会话（null）：唯一可见签也被隐藏 → 导航页兜底
    act(() => {
      useSessionStore.setState({ activeSessionId: null })
    })
    expect(screen.queryByText('内容-mine')).not.toBeInTheDocument()
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()
  })
})
