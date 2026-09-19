/**
 * WorkspacePanel 测试（ADR §5.5）
 *
 * 核验工作区 tab 切换可用：tabs 渲染、点击切换、关闭、空态。
 * ADR §5.5：WorkspacePanel 是一组可切换 tab；Splitter 已在 ChatPanelShell
 * 实现 ChatPanel↔WorkspacePanel 的拖拽分屏（§5.5 拖拽布局维度）。
 */

import { render, screen, fireEvent } from '@testing-library/react'
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { makeTab } from './helpers/workspaceTabFactory'
import type { WorkspaceTab } from '@/types/layout'

/** 渲染 WorkspacePanel（tabs/回调可注入，onFullscreen 段可选） */
function renderPanel(
  tabs: ReturnType<typeof makeTab>[],
  onTabChange: (tabId: string) => void,
  onTabClose: (tabId: string) => void,
  onFullscreen?: (fullscreen: boolean) => void,
  isFullscreen?: boolean,
  renderTabContent?: (tab: { id: string }) => React.ReactNode,
  visitedTabIds?: string[],
) {
  render(
    <WorkspacePanel
      tabs={tabs}
      onTabChange={onTabChange}
      onTabClose={onTabClose}
      renderTabContent={renderTabContent ?? (() => <div />)}
      {...(visitedTabIds !== undefined ? { visitedTabIds } : {})}
      {...(onFullscreen !== undefined ? { onFullscreen, isFullscreen } : {})}
    />,
  )
}

describe('WorkspacePanel — tab 渲染', () => {
  it('渲染所有 tab 的标题', () => {
    const tabs = [makeTab({ id: 'a', title: '编辑器' }), makeTab({ id: 'b', title: '预览', isActive: false })]
    renderPanel(tabs, () => {}, () => {})
    expect(screen.getByText('编辑器')).toBeInTheDocument()
    expect(screen.getByText('预览')).toBeInTheDocument()
  })

  it('空 tabs 渲染导航页兜底', () => {
    renderPanel([], () => {}, () => {})
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()
  })

  it('长标题 tab 悬浮显示完整标题（title 属性）', () => {
    const longTitle = 'a-very-long-file-name-that-exceeds-tab-width-config.yaml'
    renderPanel([makeTab({ id: 'a', title: longTitle })], () => {}, () => {})
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
    renderPanel(tabs, onTabChange, () => {})
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
    renderPanel(tabs, () => {}, () => {}, onFullscreen, false)
    const btn = screen.getByTestId('workspace-toggle-fullscreen')
    expect(btn).toHaveAttribute('title', '铺满全屏')
    fireEvent.click(btn)
    expect(onFullscreen).toHaveBeenCalledOnce()
  })

  it('isFullscreen=true 时按钮显示「退出全屏」', () => {
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true })]
    renderPanel(tabs, () => {}, () => {}, () => {}, true)
    expect(screen.getByTestId('workspace-toggle-fullscreen')).toHaveAttribute('title', '退出全屏')
  })
})

describe('WorkspacePanel — 内容渲染', () => {
  it('激活 tab 渲染 renderTabContent 返回的内容', () => {
    const tabs = [makeTab({ id: 'a', title: 'A', isActive: true })]
    renderPanel(tabs, () => {}, () => {}, undefined, undefined, (tab) => <div>{`内容-${tab.id}`}</div>)
    expect(screen.getByText('内容-a')).toBeInTheDocument()
  })

  it('未访问的非激活 tab 不渲染真实内容（懒挂载）', () => {
    const tabs = [
      makeTab({ id: 'a', title: 'A', isActive: true }),
      makeTab({ id: 'b', title: 'B', isActive: false }),
    ]
    renderPanel(
      tabs,
      () => {},
      () => {},
      undefined,
      undefined,
      (tab) => <div>{`内容-${tab.id}`}</div>,
      ['a'],
    )
    expect(screen.getByText('内容-a')).toBeInTheDocument()
    expect(screen.queryByText('内容-b')).not.toBeInTheDocument()
  })

  it('已访问过的非激活 tab 仍保留挂载（hidden），避免重渲染', () => {
    const tabs = [
      makeTab({ id: 'a', title: 'A', isActive: true }),
      makeTab({ id: 'b', title: 'B', isActive: false }),
    ]
    renderPanel(
      tabs,
      () => {},
      () => {},
      undefined,
      undefined,
      (tab) => <div>{`内容-${tab.id}`}</div>,
      ['a', 'b'],
    )
    // b 已访问过，内容挂载但 hidden
    const bContent = screen.getByText('内容-b')
    expect(bContent).toBeInTheDocument()
  })
})

describe('WorkspacePanel — 新建标签页按钮（浏览器式 + → 导航页签）', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('紧挨最后一个标签渲染「新建标签页」按钮（aria-label/title 齐备，位于 tablist 容器内）', () => {
    renderPanel([makeTab()], () => {}, () => {})
    const btn = screen.getByTestId('workspace-tab-new')
    expect(btn).toHaveAttribute('aria-label', '新建标签页')
    expect(btn).toHaveAttribute('title', '新建标签页')
    // 浏览器式位置：+ 在 tablist 滚动容器内（紧随各 tab 之后），不与全屏按钮一组
    expect(btn.closest('[role="tablist"]')).not.toBeNull()
  })

  it('点击 + → 导航页签落 layoutModeStore（真实 opener 链路，零 mock）', () => {
    renderPanel([makeTab()], () => {}, () => {})
    fireEvent.click(screen.getByTestId('workspace-tab-new'))

    const nav = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-panel-workspace-nav')
    expect(nav).toBeDefined()
    expect(nav?.component).toBe('workspace_nav_page')
    expect(nav?.moduleId).toBe('__panel_workspace_nav__')
    expect(nav?.isActive).toBe(true)
  })

  it('重复点击 + → 激活既有导航页签，不重复追加（openWorkspacePanel 按 id 幂等）', () => {
    renderPanel([makeTab()], () => {}, () => {})
    fireEvent.click(screen.getByTestId('workspace-tab-new'))
    fireEvent.click(screen.getByTestId('workspace-tab-new'))

    expect(
      useLayoutModeStore
        .getState()
        .workspaceTabs.filter((t) => t.id === 'ws-panel-workspace-nav'),
    ).toHaveLength(1)
  })

  it('空 tab 列表 → 按钮仍在（导航页兜底态也可新建），点击照常打开导航页签', () => {
    renderPanel([], () => {}, () => {})
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()
    expect(screen.getByTestId('workspace-tab-new')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('workspace-tab-new'))
    expect(
      useLayoutModeStore.getState().workspaceTabs.map((t) => t.id),
    ).toContain('ws-panel-workspace-nav')
  })
})
