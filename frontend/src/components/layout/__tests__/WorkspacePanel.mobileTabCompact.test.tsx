// @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
/**
 * 工作区标签行移动面板形态呼吸位（P1-2，MuMu 实测 shots/01：标签+右侧图标组
 * 无呼吸空间）。锁两点：
 * - 面板形态（无顶带槽位=移动端）：标签可压缩到 64px 下限、120px 上限，
 *   文字 truncate 省略号收边（不用渐变遮蔽——压缩态 CJK 遮罩切字）；
 * - 顶带形态（槽位在场）：既有契约不变（shrink + min 64 + fade 遮蔽）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import { makeTab } from './helpers/workspaceTabFactory'

function renderPanel(tabs: ReturnType<typeof makeTab>[]) {
  return render(
    <WorkspacePanel
      tabs={tabs}
      onTabChange={() => {}}
      onTabClose={() => {}}
      renderTabContent={() => <div />}
    />,
  )
}

describe('WorkspacePanel 标签宽界（P1-2）', () => {
  it('面板形态（移动端）：标签带压缩宽界类（min-64/max-120/shrink），文字 truncate', () => {
    const longTitle = '移动端E2E冒烟测试任务现场超长标题'
    renderPanel([makeTab({ id: 'a', title: longTitle, isActive: true })])
    const tab = screen.getByRole('tab')
    expect(tab.className).toContain('min-w-[64px]')
    expect(tab.className).toContain('max-w-[120px]')
    expect(tab.className).toContain('shrink')
    const label = screen.getByTestId('tab-label')
    expect(label.className).toContain('truncate')
    // 渐变遮蔽收边件不参与（无 data-overflowing 标记、无遮蔽 hook 状态）
    expect(label).not.toHaveAttribute('data-overflowing')
  })

  it('顶带形态（槽位在场）：既有契约不变（fade 遮蔽 + min-w-[64px]）', () => {
    const slot = document.createElement('div')
    slot.id = 'chat-top-band-workspace-tabs'
    document.body.appendChild(slot)
    try {
      const longTitle = '移动端E2E冒烟测试任务现场超长标题'
      renderPanel([makeTab({ id: 'a', title: longTitle, isActive: true })])
      const tab = screen.getByRole('tab')
      expect(tab.className).toContain('min-w-[64px]')
      expect(tab.className).toContain('max-w-[200px]')
      const label = screen.getByTestId('tab-label')
      expect(label).toHaveAttribute('data-overflowing')
      expect(label.className).not.toContain('truncate')
    } finally {
      slot.remove()
    }
  })

  it('右侧图标组（新建/只看会话）与压缩标签同排在场（让位不退场）', () => {
    const longTitle = '移动端E2E冒烟测试任务现场超长标题'
    renderPanel([makeTab({ id: 'a', title: longTitle, isActive: true })])
    expect(screen.getByTestId('workspace-tab-new')).toBeInTheDocument()
    expect(screen.getByTestId('workspace-session-filter-toggle')).toBeInTheDocument()
    expect(screen.getByTestId(`workspace-tab-a`)).toBeInTheDocument()
  })
})
