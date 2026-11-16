/** @feature FP-T12 None | @ci: frontend-test */
/** @feature 工具卡中断终态（历史孤儿工具调用显式终态） | @ci: frontend-test */
/**
 * deriveToolCallActivityStatus 显式终态范式回归。
 *
 * 背景：历史里存在「assistant 消息带 tool_calls 但对应 tool 结果消息从未落库」
 * 的孤儿（run 被中断时工具未返回）。旧规则「数据缺席 = 进行中」使孤儿卡永动
 * 转圈/计时（实证：一张卡计时走到 12h 9m 不消失）。
 *
 * 契约（Vercel AI SDK 工具部件显式状态机同构：进行中只能由持久化状态在活跃
 * 流式上下文中声明，不得由数据缺席推导）：
 * - 终态证据（错误/结果）在场 → 由证据定终态，任何上下文不回退；
 * - 显式 cancelled → cancelled；
 * - 活跃流式上下文中的在途 state → running（真在途不误判）；
 * - 非流式上下文 + 无任何终态证据的在途 state → interrupted（中断终态）。
 */
import { describe, expect, it } from 'vitest'
import { deriveToolCallActivityStatus } from '../activity'

describe('deriveToolCallActivityStatus（显式终态范式）', () => {
  it('终态证据优先：错误在场 → failed，与上下文无关', () => {
    expect(deriveToolCallActivityStatus({ state: 'error', hasError: true }, true)).toBe('failed')
    expect(deriveToolCallActivityStatus({ state: 'error', hasError: true }, false)).toBe('failed')
  })

  it('终态证据优先：结果/结构化结果在场 → completed，任何上下文不回退', () => {
    expect(deriveToolCallActivityStatus({ state: 'done', hasResult: true }, false)).toBe('completed')
    // 在途 state 撞上迟到结果：以证据为准（tool_start 与 tool_result 竞态不回退）
    expect(deriveToolCallActivityStatus({ state: 'calling', hasResult: true }, true)).toBe('completed')
    // resultData 单独在场（截断预览形态）同样算终态证据
    expect(deriveToolCallActivityStatus({ state: 'streaming', hasResult: true }, false)).toBe('completed')
  })

  it('显式 cancelled → cancelled（用户主动取消，非中断）', () => {
    expect(deriveToolCallActivityStatus({ state: 'cancelled' }, true)).toBe('cancelled')
    expect(deriveToolCallActivityStatus({ state: 'cancelled' }, false)).toBe('cancelled')
  })

  it('活跃流式上下文中的在途 state → running（现状保留）', () => {
    expect(deriveToolCallActivityStatus({ state: 'calling' }, true)).toBe('running')
    expect(deriveToolCallActivityStatus({ state: 'streaming' }, true)).toBe('running')
  })

  it('非流式上下文 + 在途 state 无终态证据 → interrupted（孤儿工具调用显式终态）', () => {
    expect(deriveToolCallActivityStatus({ state: 'calling' }, false)).toBe('interrupted')
    expect(deriveToolCallActivityStatus({ state: 'streaming' }, false)).toBe('interrupted')
  })
})
