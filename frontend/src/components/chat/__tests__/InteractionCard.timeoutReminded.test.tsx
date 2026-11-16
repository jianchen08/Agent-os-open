/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * 审批卡超时提醒态渲染（P-20）：timeoutReminded 置位后倒计时徽章切
 * 「即将超时」warning 高亮；未提醒保持既有 muted 形态（BUG-14 基线回归）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '@/components/chat/InteractionCard'
import { cardProps, makeInteraction } from './interactionCardTestUtils'

function renderCountdownCard(overrides: Parameters<typeof makeInteraction>[0]) {
  render(<InteractionCard {...cardProps(makeInteraction(overrides))} />)
  return screen.getByTestId('approval-countdown')
}

describe('InteractionCard 超时提醒态（P-20）', () => {
  it('timeoutReminded 置位 → 徽章切「即将超时」前缀 + warning 高亮类', () => {
    const badge = renderCountdownCard({
      title: '安全审批: bash_execute',
      timeoutSeconds: 600,
      createdAt: new Date().toISOString(),
      timeoutReminded: true,
      options: [{ id: 'approved_once', label: '仅本次执行' }],
    })

    expect(badge.textContent).toContain('即将超时')
    expect(badge.textContent).toContain('剩余')
    expect(badge.className).toContain('badge-warning-bg')
    expect(badge.className).not.toContain('badge-danger-bg')
  })

  it('未提醒 → 既有形态不变（「剩余」muted 徽章，BUG-14 基线回归）', () => {
    const badge = renderCountdownCard({
      title: '安全审批: bash_execute',
      timeoutSeconds: 600,
      createdAt: new Date().toISOString(),
      options: [{ id: 'approved_once', label: '仅本次执行' }],
    })

    expect(badge.textContent).toMatch(/^剩余 /)
    expect(badge.textContent).not.toContain('即将超时')
    expect(badge.className).toContain('bg-muted')
    expect(badge.className).not.toContain('badge-warning-bg')
  })

  it('已超时态优先于提醒态（danger 判定不被 timeoutReminded 覆盖）', () => {
    vi.useFakeTimers()
    try {
      const created = new Date(Date.now() - 700_000).toISOString()
      const badge = renderCountdownCard({
        title: '安全审批: bash_execute',
        timeoutSeconds: 600,
        createdAt: created,
        timeoutReminded: true,
        options: [{ id: 'approved_once', label: '仅本次执行' }],
      })

      expect(badge.textContent).toContain('已超时，将自动拒绝')
      expect(badge.className).toContain('badge-danger-bg')
    } finally {
      vi.useRealTimers()
    }
  })
})
