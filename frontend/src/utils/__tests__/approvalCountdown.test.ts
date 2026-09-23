// @feature: FP-T12 前端适配——BUG-43 交互卡不拦截 composer（收起徽标倒计时与卡内倒计时共用纯工具） | @ci: frontend-test
/**
 * 审批等待倒计时纯工具测试
 *
 * 消费方：InteractionCard 卡内倒计时（BUG-14 有界等待可见化）、
 * GlobalInteractionOverlay 收起徽标（BUG-43）。均为纯函数，直测输入→输出。
 */
import { describe, expect, it } from 'vitest'
import { approvalDeadlineMs, formatRemaining } from '../approvalCountdown'

describe('formatRemaining 剩余时间格式', () => {
  it.each([
    [0, '0:00'],
    [9, '0:09'],
    [59, '0:59'],
    [60, '1:00'],
    [600, '10:00'],
    [3599, '59:59'],
    [3600, '1:00:00'],
    [86399, '23:59:59'],
    [86400, '24:00:00'],
  ])('%i 秒 → %s（<1h 为 m:ss，≥1h 为 h:mm:ss）', (seconds, expected) => {
    expect(formatRemaining(seconds)).toBe(expected)
  })

  it('性质：输出可无损回读为同一秒数（小时/分钟/秒三个量级方向）', () => {
    for (const s of [1, 61, 3599, 3661, 86399, 90061]) {
      const parts = formatRemaining(s).split(':').map(Number)
      const total =
        parts.length === 3
          ? parts[0] * 3600 + parts[1] * 60 + parts[2]
          : parts[0] * 60 + parts[1]
      expect(total).toBe(s)
    }
  })
})

describe('approvalDeadlineMs 审批等待截止时刻', () => {
  const BASE = '2026-09-18T00:00:00Z'

  // BUG-60（用户裁定 2026-09-22 推翻 cap 方案）：审批族超时统一 24h——内核
  // mcp client 默认 86400s 与 human create_choice 声明对齐，声明值端到端生效，
  // 截止时刻 = 基准 + 声明值原样透传（无 min cap 收敛）
  it.each([
    [600, '声明 600s（短窗显式声明）→ 原样透传'],
    [86400, '声明 86400s（审批族统一 24h 常量）→ 原样透传'],
    [120000, '声明 120000s（装机版历史值）→ 原样透传'],
  ])('timeoutSeconds=%i → 截止 = 基准 + 声明值（%s）', (declared) => {
    expect(
      approvalDeadlineMs({ createdAt: BASE, timestamp: BASE, timeoutSeconds: declared }),
    ).toBe(Date.parse(BASE) + declared * 1000)
  })

  it('统一常量断言：声明 86400s（审批族统一 24h）截止恰为基准 + 24h', () => {
    expect(approvalDeadlineMs({ createdAt: BASE, timestamp: BASE, timeoutSeconds: 86400 })).toBe(
      Date.parse(BASE) + 86_400_000,
    )
  })

  it('声明窗 60s → 按声明值计算（区分度输入）', () => {
    expect(approvalDeadlineMs({ createdAt: BASE, timestamp: BASE, timeoutSeconds: 60 })).toBe(
      Date.parse(BASE) + 60_000,
    )
  })

  it('createdAt 缺失回退 timestamp（区分度输入）', () => {
    expect(approvalDeadlineMs({ timestamp: BASE, timeoutSeconds: 60 })).toBe(
      Date.parse(BASE) + 60_000,
    )
  })

  it('timeoutSeconds 缺失 → 截止即基准（是否显示倒计时由消费方按 timeoutSeconds>0 把关）', () => {
    expect(approvalDeadlineMs({ timestamp: BASE })).toBe(Date.parse(BASE))
  })

  it('时间字段缺失/不可解析 → null（不显示倒计时）', () => {
    expect(approvalDeadlineMs({ timestamp: '' })).toBeNull()
    expect(approvalDeadlineMs({ timestamp: 'not-a-date' })).toBeNull()
  })
})
