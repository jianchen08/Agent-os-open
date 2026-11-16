/** @feature FP-T12 None | @ci: frontend-test */
/** @feature 工具卡中断终态（历史孤儿工具调用显式终态） | @ci: frontend-test */
/**
 * ActivityCard 中断终态卡渲染回归（静止、零计时器）。
 *
 * 契约：
 * - interrupted 卡：头部标注「已中断（无结果）」、无转圈、无时长徽标；
 *   fake timers 推进 12 小时 + 系统时间前移后重渲染，可见文案与时长显示零变化
 *   （「进行中」不得由数据缺席推导 → 不存在随时间增长的计时面）。
 * - running 卡对照：转圈在场（流式现状不回归）。
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import type { ActivityData } from '@/types/activity'

function makeActivity(status: ActivityData['status'], extra?: Partial<ActivityData>): ActivityData {
  return {
    type: 'tool_call',
    id: 'call_1c7baf51',
    toolName: 'human_interaction',
    title: '人工交互',
    status,
    ...extra,
  }
}

beforeEach(() => {
  vi.useFakeTimers({ now: new Date('2026-10-24T08:00:00.000Z') })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ActivityCard 中断终态卡（静止、零计时器）', () => {
  it('interrupted 卡：标注文本可见、无转圈节点、状态属性落 DOM', () => {
    const { container } = render(
      <ActivityCard activity={makeActivity('interrupted', { statusText: '已中断（无结果）' })} />,
    )
    expect(screen.getByText('已中断（无结果）')).toBeInTheDocument()
    expect(container.querySelector('.animate-spin')).toBeNull()
    const card = container.querySelector('[data-activity-status="interrupted"]')
    expect(card).not.toBeNull()
  })

  it('fake timers 推进 12 小时 + 重渲染：可见文案零增长（无永动计时面）', () => {
    const { container, rerender } = render(
      <ActivityCard activity={makeActivity('interrupted', { statusText: '已中断（无结果）' })} />,
    )
    const before = container.textContent
    expect(before).toContain('已中断（无结果）')
    act(() => {
      vi.advanceTimersByTime(12 * 60 * 60 * 1000 + 9 * 60 * 1000)
    })
    rerender(<ActivityCard activity={makeActivity('interrupted', { statusText: '已中断（无结果）' })} />)
    // 文本零变化：既无新增时长（h/m 形态），也不改变既有标注
    expect(container.textContent).toBe(before)
    expect(container.textContent).not.toMatch(/\d+h/)
  })

  it('interrupted 卡无时长输入时不伪造时长徽标', () => {
    const { container } = render(<ActivityCard activity={makeActivity('interrupted')} />)
    expect(container.textContent).not.toMatch(/\d+(\.\d+)?(ms|s)\b/)
  })

  it('running 卡对照：转圈在场（流式现状不回归）', () => {
    const { container } = render(<ActivityCard activity={makeActivity('running')} />)
    expect(container.querySelector('.animate-spin')).not.toBeNull()
  })

  it('completed 卡对照：无中断标注（statusText 缺省不出现在其他状态）', () => {
    render(<ActivityCard activity={makeActivity('completed')} />)
    expect(screen.queryByText('已中断（无结果）')).not.toBeInTheDocument()
  })
})
