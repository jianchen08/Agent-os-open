/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 顶带设计系统行为契约（bandButton 单源 + 标签压缩省略）
 *
 * 用户裁定（2026-09-21）：
 * - 顶带所有按钮——窗口控制、区域开关、对话/工作区标签——同高度、同圆角、
 *   同间隔；宽度由内容（文字长度 + 图标宽度）决定，不写死。
 * - 标签太挤时文字随宽度省略（少显示几个字），而不是溢出或挤爆相邻元素。
 * - 按钮不顶到窗口边缘（顶带两端留白）。
 * - 按钮以外区域可拖动窗口。
 *
 * 断行为不断实现：只断言可观察契约——标签允许收缩且有下限与上限、文字节点带
 * 省略前提、顶带按钮间隔来自同一单源（改一处即可全带变化）、边缘留白存在。
 * 不复制类名字符串到断言里（那是实现细节），而是断言单源导出的常量确实生效。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentTabBar, type AgentTab } from '@/components/chat/AgentTabBar'
import { AgentTabItem } from '@/components/chat/AgentTabItem'
import {
  BAND_BUTTON_CLASS,
  BAND_EDGE_PADDING_CLASS,
  BAND_GAP_CLASS,
  BAND_ICON_BUTTON_CLASS,
  BAND_TAB_MAX_WIDTH_CLASS,
  BAND_TAB_MIN_WIDTH_CLASS,
  BAND_TAB_WIDTH_CLASS,
} from '@/components/layout/bandButton'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import type { WorkspaceTab } from '@/types/layout'

/** 标签宽度契约的可观察断言（对话/工作区标签共用）：可压缩、有上下限 */
function expectTabShrinkable(tab: HTMLElement): void {
  expect(tab.className).toContain('shrink')
  expect(tab.className).not.toContain('shrink-0')
  expect(tab.className).toMatch(/min-w-\[/)
  expect(tab.className).toMatch(/max-w-\[/)
}

/** 单源契约：类名常量必须真实声明各维度，改值即全带同步 */
describe('bandButton 单源', () => {
  it('按钮基类声明高度/圆角/内边距，且不含 shrink 决策（由使用方决定）', () => {
    expect(BAND_BUTTON_CLASS).toMatch(/\bh-7\b/)
    expect(BAND_BUTTON_CLASS).toMatch(/\brounded-md\b/)
    expect(BAND_BUTTON_CLASS).toMatch(/\bpx-2\b/)
    expect(BAND_BUTTON_CLASS).not.toMatch(/\bshrink-0\b/)
    expect(BAND_BUTTON_CLASS).not.toMatch(/\bw-\d/)
  })

  it('图标独占按钮固定宽度且不参与压缩；间隔与边缘留白各自单值', () => {
    expect(BAND_ICON_BUTTON_CLASS).toMatch(/\bw-7\b/)
    expect(BAND_ICON_BUTTON_CLASS).toMatch(/\bshrink-0\b/)
    expect(BAND_GAP_CLASS).toBe('gap-1')
    expect(BAND_EDGE_PADDING_CLASS).toBe('px-2')
  })

  it('标签宽度契约：可压缩 + 有下限 + 有上限', () => {
    expect(BAND_TAB_WIDTH_CLASS).toMatch(/\bshrink\b/)
    expect(BAND_TAB_WIDTH_CLASS).not.toMatch(/\bshrink-0\b/)
    expect(BAND_TAB_MIN_WIDTH_CLASS).toMatch(/\bmin-w-\[/)
    expect(BAND_TAB_MAX_WIDTH_CLASS).toMatch(/\bmax-w-\[/)
  })
})

/** 对话标签：压缩省略可观察契约 */
describe('对话标签压缩省略', () => {
  const makeTab = (over?: Partial<AgentTab>): AgentTab => ({
    id: 't1',
    name: '一个非常非常长的任务标题应该被省略号截断',
    status: 'running',
    isActive: false,
    canClose: true,
    agentLevel: 2,
    ...over,
  })

  it('长标题标签可压缩（非 shrink-0）且带宽度上下限——挤压时省略而非溢出', () => {
    render(<AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} />)
    expectTabShrinkable(screen.getByRole('tab'))
  })

  it('标题文字节点可收缩且硬裁切（溢出用渐变遮蔽收边，不用省略号），全文可经 title 读取', () => {
    render(<AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} />)
    const tab = screen.getByRole('tab')
    const text = screen.getByTestId('tab-label')
    expect(text).not.toBeNull()
    // min-w-0：flex item 默认 min-width:auto 会让裁切失效，必须显式归零
    expect(text.className).toContain('min-w-0')
    expect(text.className).toContain('overflow-hidden')
    expect(text.textContent).toBe('一个非常非常长的任务标题应该被省略号截断')
    expect(tab.getAttribute('title')).toBe('一个非常非常长的任务标题应该被省略号截断')
  })

  it('短标题与长标题共用同一宽度契约（宽度由内容定，不写死）', () => {
    const { rerender } = render(<AgentTabBar tabs={[makeTab({ name: '短' })]} onTabChange={() => {}} />)
    const shortCls = screen.getByRole('tab').className
    rerender(<AgentTabBar tabs={[makeTab()]} onTabChange={() => {}} />)
    const longCls = screen.getByRole('tab').className
    expect(longCls).toBe(shortCls)
  })

  it('标签不参与拖拽窗口（app-no-drag），可点击切换', () => {
    let clicked = ''
    render(<AgentTabBar tabs={[makeTab()]} onTabChange={(id) => (clicked = id)} />)
    const tab = screen.getByRole('tab')
    expect(tab.className).toContain('app-no-drag')
    tab.click()
    expect(clicked).toBe('t1')
  })
})

/** 工作区标签：同一宽度契约 + 行内纵向滚动锁 */
describe('工作区标签压缩省略与滚动锁', () => {
  const tabs: WorkspaceTab[] = [
    {
      id: 'w1',
      title: '一个非常非常长的工作区页面标题应该被省略号截断',
      isActive: false,
      isPinned: true,
      component: 'nav',
    },
  ]
  const renderPanel = (list: WorkspaceTab[]) =>
    render(
      <WorkspacePanel
        tabs={list}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => null}
      />,
    )

  it('长标题标签可压缩且有上下限，文字节点具备遮蔽收边前提', () => {
    renderPanel(tabs)
    expectTabShrinkable(screen.getByRole('tab'))
    const text = screen.getByTestId('tab-label')
    expect(text.className).toContain('min-w-0')
  })

  it('标签行横向可滚但纵向锁定（不出现上下滚轮）', () => {
    renderPanel(tabs)
    const list = screen.getByRole('tablist')
    expect(list.className).toContain('overflow-x-auto')
    expect(list.className).toContain('overflow-y-hidden')
  })

  it('标签行间隔取自单源（全带一处可改）', () => {
    renderPanel(tabs)
    expect(screen.getByRole('tablist').className).toContain(BAND_GAP_CLASS)
  })
})

/** 对话标签栏：行内间隔同样取单源 */
describe('对话标签栏间隔单源', () => {
  it('标签行与标签栏容器都使用统一间隔', () => {
    render(<AgentTabBar tabs={[{ id: 'a', name: '主管道', status: 'running', isActive: true, canClose: false, agentLevel: 1 }]} onTabChange={() => {}} />)
    expect(screen.getByRole('tablist').className).toContain(BAND_GAP_CLASS)
    expect(screen.getByTestId('agent-tab-bar').className).toContain(BAND_GAP_CLASS)
  })
})

/** 单条标签组件单独渲染时也遵守宽度契约（防止有人只改 bar 不改 item） */
describe('AgentTabItem 独立宽度契约', () => {
  it('独立渲染时同样可压缩 + 遮蔽收边前提', () => {
    render(
      <AgentTabItem
        tab={{
          id: 'x',
          name: '独立的超长标签标题需要省略显示',
          agentLevel: 2,
          status: 'running',
          isActive: false,
          canClose: true,
        }}
        onClick={() => {}}
      />,
    )
    const tab = screen.getByRole('tab')
    expect(tab.className).toContain('shrink')
    expect(tab.className).not.toContain('shrink-0')
    const text = screen.getByTestId('tab-label')
    expect(text.className).toContain('min-w-0')
  })
})
