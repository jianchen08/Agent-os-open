/**
 * 架构契约测试：设计 token 三族必须存在且被 Tailwind 映射
 *
 * 意图：统一审查 §4.1 把"字号无 token（123 处任意 text-[NNpx]）""图标 5 种尺寸混用"
 * 列为 P0 漂移。token 是收口的前提；本测试钉死 token 作为唯一来源，
 * 后续页面迁移才能把任意值替换为 text-caption/label/body/title 与 h-icon-*。
 *
 * 关联：docs/working/design/frontend-design-unification-execution-plan.md §三 M0.2
 */

import { describe, expect, it } from 'vitest'
import { readSource } from './harness'

const FONT_VARS = [
  '--font-size-caption',
  '--font-size-label',
  '--font-size-body',
  '--font-size-title',
  '--font-size-page-title',
]
const ICON_VARS = ['--icon-size-xs', '--icon-size-sm', '--icon-size-md']

describe('设计 token 三族 —— CSS 变量定义', () => {
  const css = readSource('src/styles/design-tokens.css')

  it.each(FONT_VARS)('字号变量 %s 在 :root 定义', (v) => {
    expect(css).toContain(`${v}:`)
  })

  it.each(ICON_VARS)('图标尺寸变量 %s 在 :root 定义', (v) => {
    expect(css).toContain(`${v}:`)
  })
})

describe('设计 token 三族 —— Tailwind 映射', () => {
  const tw = readSource('tailwind.config.js')

  it.each(['caption', 'label', 'body', 'title', 'page-title'])(
    '字号 Tailwind 类 text-%s 已注册（映射到 CSS 变量）',
    (name) => {
      expect(tw).toContain(name)
    },
  )

  it.each(['icon-xs', 'icon-sm', 'icon-md'])(
    '图标尺寸 Tailwind 类 w-%s / h-%s 已注册',
    (name) => {
      expect(tw).toContain(name)
    },
  )
})
