// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** safeLoadLayout：缺省回退默认配置；合并抛错（异常 getter）→ 默认配置兜底 */
import { describe, expect, it } from 'vitest'
import { DEFAULT_LAYOUT_CONFIG, safeLoadLayout } from '../resolver'

describe('safeLoadLayout', () => {
  it('undefined → 默认配置', () => {
    expect(safeLoadLayout(undefined)).toEqual(DEFAULT_LAYOUT_CONFIG)
  })

  it('正常主题布局：逐域合并，缺省域落默认值', () => {
    const merged = safeLoadLayout({ sidebar: { width: 300 } } as never)
    expect(merged.sidebar.width).toBe(300)
    expect(merged.chatPanel).toEqual(DEFAULT_LAYOUT_CONFIG.chatPanel)
  })

  it('字段 getter 抛错（病态主题配置）→ 默认配置兜底不崩', () => {
    const evil = {
      get breakpoints() {
        throw new Error('boom')
      },
    } as never
    expect(safeLoadLayout(evil)).toEqual(DEFAULT_LAYOUT_CONFIG)
  })
})
