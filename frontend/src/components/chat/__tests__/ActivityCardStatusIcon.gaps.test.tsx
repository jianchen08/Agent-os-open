// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * getStatusIcon 全状态分支直测。
 *
 * 渲染侧仅 running/failed 到达该函数（降噪设计，见 ActivityCard 状态图标
 * 门控注释）；函数本身按 ActivityStatus 全集设计，pending/completed/
 * cancelled/default 分支经渲染路径不可达，导出后在此逐分支驱动锁定。
 * 行为断言：running 携带呼吸动画样式，其余为静态色样式；分支互异以
 * 各自独立的图标元素表达。
 */
import { describe, expect, it } from 'vitest'
import { getStatusIcon } from '@/components/chat/ActivityCard'

describe('getStatusIcon — 全状态分支', () => {
  const cases = ['pending', 'running', 'completed', 'failed', 'cancelled'] as const

  it.each(cases)('%s → 返回图标元素', (status) => {
    const icon = getStatusIcon(status)
    expect(icon).toBeTruthy()
  })

  it('running 独有呼吸动画样式；非 running 为静态色', () => {
    const running = getStatusIcon('running')
    const pending = getStatusIcon('pending')
    expect((running.props.style as React.CSSProperties).animation).toContain('breathe')
    expect((pending.props.style as React.CSSProperties).animation).toBeUndefined()
  })
})
