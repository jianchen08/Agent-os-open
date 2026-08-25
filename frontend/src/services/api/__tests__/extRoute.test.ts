/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
import { describe, it, expect } from 'vitest'
import { EXT_ROUTE, extUrl } from '../extRoute'

describe('extRoute 插件 ext 路由拼接', () => {
  it('EXT_ROUTE 为内核 ext 命名空间前缀（不带尾斜杠）', () => {
    expect(EXT_ROUTE).toBe('/ext')
  })

  it('path 带首斜杠直拼，缺首斜杠自动补齐（幂等等价）', () => {
    expect(extUrl('demo_plugin', '/assets/a.css')).toBe('/ext/demo_plugin/assets/a.css')
    expect(extUrl('demo_plugin', 'assets/a.css')).toBe('/ext/demo_plugin/assets/a.css')
  })

  it('任意 pluginId 均按声明驱动拼接（不枚举具体插件）', () => {
    expect(extUrl('p1', '/webview')).toBe('/ext/p1/webview')
    expect(extUrl('p2', '/styles/skin/dark/merged.css')).toBe('/ext/p2/styles/skin/dark/merged.css')
  })
})
