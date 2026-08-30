/**
 * WorkspacePanel 测试（ADR §5.5）
 *
 * 核验工作区 tab 切换可用：tabs 渲染、点击切换、关闭、空态。
 * ADR §5.5：WorkspacePanel 是一组可切换 tab；Splitter 已在 ChatPanelShell
 * 实现 ChatPanel↔WorkspacePanel 的拖拽分屏（§5.5 拖拽布局维度）。
 */

import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import type { WorkspaceTab } from '@/types/layout'

function makeTab(overrides: Partial<WorkspaceTab> = {}): WorkspaceTab {
  return {
    id: 'tab-1',
    title: '标签1',
    isActive: true,
    isPinned: false,
    ...overrides,
  } as WorkspaceTab
}

describe('WorkspacePanel — tab 渲染', () => {
  it('渲染所有 tab 的标题', () => {
    const tabs = [makeTab({ id: 'a', title: '编辑器' }), makeTab({ id: 'b', title: '预览', isActive: false })]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => <div>content</div>}
      />,
    )
    expect(screen.getByText('编辑器')).toBeInTheDocument()
    expect(screen.getByText('预览')).toBeInTheDocument()
  })

  it('空 tabs 显示空态提示', () => {
    render(
      <WorkspacePanel
        tabs={[]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => <div />}
      />,
    )
    expect(screen.getByText(/暂无内容/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /打开任务管理/ })).toBeInTheDocument()
  })

  it('长标题 tab 悬浮显示完整标题（title 属性）', () => {
    const longTitle = 'a-very-long-file-name-that-exceeds-tab-width-config.yaml'
    render(
      <WorkspacePanel
        tabs={[makeTab({ id: 'a', title: longTitle })]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => <div>content</div>}
      />,
    )
    expect(screen.getByRole('tab')).toHaveAttribute('title', longTitle)
  })
})

describe('WorkspacePanel — tab 切换', () => {
  it('点击非激活 tab 触发 onTabChange（带 tabId）', () => {
    const onTabChange = vi.fn()
    const tabs = [
      makeTab({ id: 'a', title: 'A', isActive: true }),
      makeTab({ id: 'b', title: 'B', isActive: false }),
    ]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={onTabChange}
        onTabClose={() => {}}
        renderTabContent={() => <div />}
      />,
    )
    fireEvent.click(screen.getByText('B'))
    expect(onTabChange).toHaveBeenCalledWith('b')
  })

  it('点击关闭按钮触发 onTabClose 且不冒泡到 onTabChange', () => {
    const onTabChange = vi.fn()
    const onTabClose = vi.fn()
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true, isPinned: false })]
    const { container } = render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={onTabChange}
        onTabClose={onTabClose}
        renderTabContent={() => <div />}
      />,
    )
    // 用 data-testid 精确定位关闭按钮（避免被 maximize/fullscreen 按钮干扰）
    const closeBtn = container.querySelector('[data-testid="workspace-tab-close-a"]')
    expect(closeBtn).not.toBeNull()
    fireEvent.click(closeBtn!)
    expect(onTabClose).toHaveBeenCalledWith('a')
    expect(onTabChange).not.toHaveBeenCalled()
  })

  it('pinned tab 不显示关闭按钮', () => {
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true, isPinned: true })]
    const { container } = render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => <div />}
      />,
    )
    // pinned tab 无关闭按钮，也无 maximize/fullscreen（未传对应回调）
    expect(container.querySelector('[data-testid^="workspace-tab-close-"]')).toBeNull()
  })
})

describe('WorkspacePanel — 全屏按钮', () => {
  it('传入 onFullscreen 时渲染全屏按钮并触发回调', () => {
    const onFullscreen = vi.fn()
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true })]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => <div />}
        onFullscreen={onFullscreen}
        isFullscreen={false}
      />,
    )
    const btn = screen.getByTestId('workspace-toggle-fullscreen')
    expect(btn).toHaveAttribute('title', '铺满全屏')
    fireEvent.click(btn)
    expect(onFullscreen).toHaveBeenCalledOnce()
  })

  it('isFullscreen=true 时按钮显示「退出全屏」', () => {
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true })]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => <div />}
        onFullscreen={() => {}}
        isFullscreen={true}
      />,
    )
    expect(screen.getByTestId('workspace-toggle-fullscreen')).toHaveAttribute('title', '退出全屏')
  })
})

describe('WorkspacePanel — 内容渲染', () => {
  it('激活 tab 渲染 renderTabContent 返回的内容', () => {
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true })]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
      />,
    )
    expect(screen.getByText('内容-a')).toBeInTheDocument()
  })

  it('未访问的非激活 tab 不渲染真实内容（懒挂载）', () => {
    const tabs = [
      makeTab({ id: 'a', title: 'A', isActive: true }),
      makeTab({ id: 'b', title: 'B', isActive: false }),
    ]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
        visitedTabIds={['a']}
      />,
    )
    expect(screen.getByText('内容-a')).toBeInTheDocument()
    expect(screen.queryByText('内容-b')).not.toBeInTheDocument()
  })

  it('已访问过的非激活 tab 仍保留挂载（hidden），避免重渲染', () => {
    const tabs = [
      makeTab({ id: 'a', title: 'A', isActive: true }),
      makeTab({ id: 'b', title: 'B', isActive: false }),
    ]
    render(
      <WorkspacePanel
        tabs={tabs}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
        visitedTabIds={['a', 'b']}
      />,
    )
    // b 已访问过，内容挂载但 hidden
    const bContent = screen.getByText('内容-b')
    expect(bContent).toBeInTheDocument()
  })
})
