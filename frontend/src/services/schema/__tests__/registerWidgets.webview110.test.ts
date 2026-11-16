/** @feature 双端优化轮 R22 | @ci: frontend-test */
/**
 * registerWidgets — WebView 110（Chromium <111）防回归测试（R12 审计 P2 清理）
 *
 * csv_table 表头淡底：color-mix 单声明在 110 解析失败 → 整条声明丢弃、表头
 * tint 丢失。
 * 契约：双声明 fallback——var(--muted) 主题 token 在前（无 color-mix 内核
 * 保底，随主题），currentColor 6% 的 color-mix 在后（支持内核覆盖着色）；
 * 顺序不可倒置（cascade 后者胜）。
 */

import { describe, expect, it } from 'vitest'
import { FIRST_PARTY_CONTENT_CARDS } from '@/services/schema/registerWidgets'

describe('registerWidgets · WebView 110 兼容（csv_table 表头淡底双声明）', () => {
  it('thead 淡底为双声明：var(--muted) fallback 在前、color-mix 在后覆盖', () => {
    const css = FIRST_PARTY_CONTENT_CARDS.find((c) => c.id === 'csv_table')?.css ?? ''
    expect(css).toContain(
      'thead { background: var(--muted); background: color-mix(in srgb, currentColor 6%, transparent); }',
    )
  })

  it('双声明顺序契约：color-mix 声明必须位于 var(--muted) 声明之后', () => {
    const css = FIRST_PARTY_CONTENT_CARDS.find((c) => c.id === 'csv_table')?.css ?? ''
    const fallbackAt = css.indexOf('background: var(--muted)')
    const mixAt = css.indexOf('color-mix(')
    expect(fallbackAt).toBeGreaterThanOrEqual(0)
    expect(mixAt).toBeGreaterThan(fallbackAt)
  })
})
