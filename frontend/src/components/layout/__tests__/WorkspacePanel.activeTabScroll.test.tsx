/** @feature FP-0.2.移动端适配 M20 激活页签滚动可见 @vision V6 可即用 @ci frontend-test */
/**
 * WorkspacePanel 激活页签滚动可见测试
 *
 * 契约：页签条横向可滚（overflow-x-auto），窄幅视口（移动端）或页签较多时
 * 激活签可能被裁在条外——激活签变化时必须把新激活签滚回可见区（scrollIntoView
 * block/inline nearest，只动页签条不牵动页面），页签条反馈与内容保持一致；
 * 激活签不变的重渲染不重复触发滚动。
 */

import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import { makeTab } from './helpers/workspaceTabFactory'
import type { WorkspaceTab } from '@/types/layout'

function renderPanelOnce(tabs: Array<Partial<WorkspaceTab> & { id: string }>) {
  return render(
    <WorkspacePanel
      tabs={tabs.map((t) => makeTab(t))}
      onTabChange={() => {}}
      onTabClose={() => {}}
      renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
    />,
  )
}

/** 对指定页签元素发生的 scrollIntoView 调用（contexts 记录 this） */
function scrollCallsOn(el: Element) {
  const scrollMock = Element.prototype.scrollIntoView as unknown as {
    mock: { contexts: unknown[]; calls: Array<Array<unknown>> }
  }
  return scrollMock.mock.contexts
    .map((ctx, i) => ({ ctx, args: scrollMock.mock.calls[i] }))
    .filter((c) => c.ctx === el)
}

describe('WorkspacePanel — 激活页签滚动可见', () => {
  beforeEach(() => {
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: vi.fn(),
    })
  })

  it('激活签变化 → 新激活签被滚回可见区（nearest，不牵动纵向）', () => {
    const { rerender } = renderPanelOnce([
      { id: 'a', title: '甲', isActive: true },
      { id: 'b', title: '乙', isActive: false },
      { id: 'c', title: '丙', isActive: false },
    ])
    const tabC = screen.getByTestId('workspace-tab-c')
    expect(scrollCallsOn(tabC)).toHaveLength(0)
    rerender(
      <WorkspacePanel
        tabs={[
          makeTab({ id: 'a', title: '甲', isActive: false }),
          makeTab({ id: 'b', title: '乙', isActive: false }),
          makeTab({ id: 'c', title: '丙', isActive: true }),
        ]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
      />,
    )
    const calls = scrollCallsOn(tabC)
    expect(calls).toHaveLength(1)
    expect(calls[0].args).toEqual([{ block: 'nearest', inline: 'nearest' }])
  })

  it('激活签不变的重渲染不重复滚动（幂等）', () => {
    const { rerender } = renderPanelOnce([
      { id: 'a', title: '甲', isActive: true },
      { id: 'b', title: '乙', isActive: false },
    ])
    const tabA = screen.getByTestId('workspace-tab-a')
    const before = scrollCallsOn(tabA).length
    rerender(
      <WorkspacePanel
        tabs={[
          makeTab({ id: 'a', title: '甲', isActive: true }),
          makeTab({ id: 'b', title: '乙', isActive: false }),
        ]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={(tab) => <div>{`内容-${tab.id}`}</div>}
      />,
    )
    expect(scrollCallsOn(tabA)).toHaveLength(before)
  })
})
