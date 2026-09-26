/** @feature FP-T12 插件 CSS 白名单消毒 | @ci: frontend-test */
/**
 * sanitizeCss 白名单化（2026-09-25 准入分级波2）
 *
 * 行为契约（fail-closed，命中整段拒绝）：
 * - url() 仅放行 data: 与相对路径（同源 /ext 资产、SVG 片段 url(#id)）；
 *   scheme:// 与协议相对 // 一律拒绝
 * - @import 一律禁止（构建期合并为唯一正道）
 * - 保留既有危险构造拦截（expression/javascript:/behavior: 等）
 * - injectPluginStyle 体积上限 64KB
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { apiClient } from '@/services/api/client'
import { MAX_CLIENT_STYLE_BYTES, injectPluginStyle, sanitizeCss } from '../pluginStyles'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/services/api/extRoute', () => ({
  extUrl: (pluginId: string, path: string) => `/ext/${pluginId}${path}`,
  EXT_ROUTE: '/ext',
}))

const apiGet = apiClient.get as unknown as ReturnType<typeof vi.fn>

describe('sanitizeCss · url() 白名单', () => {
  it('data: 与相对路径放行（同源资产 + SVG 片段引用）', () => {
    expect(sanitizeCss('.a { background: url(data:image/png;base64,AAAA) }')).not.toBeNull()
    expect(sanitizeCss('.a { background: url(/ext/p/assets/x.png) }')).not.toBeNull()
    expect(sanitizeCss('.a { background: url("assets/x.png") }')).not.toBeNull()
    expect(sanitizeCss('.a { filter: url(#gold) }')).not.toBeNull()
  })

  it('绝对地址整段拒绝：scheme:// 与协议相对 //（≥2 组区分输入）', () => {
    expect(sanitizeCss('.a { background: url(https://evil.example/i.png) }')).toBeNull()
    expect(sanitizeCss(".a { background: url('http://evil.example/i.png') }")).toBeNull()
    expect(sanitizeCss('.a { background: url(//tracker.example/i.png) }')).toBeNull()
    expect(sanitizeCss('.a { background: url(blob:https://x/y) }')).toBeNull()
  })

  it('@import 一律禁止（含相对路径拆分——构建期合并为唯一正道）', () => {
    expect(sanitizeCss('@import "more.css";')).toBeNull()
    expect(sanitizeCss('@import url(./more.css);')).toBeNull()
    expect(sanitizeCss('@import url(https://evil.example/x.css);')).toBeNull()
  })

  it('既有危险构造保留拦截（回归锁定）', () => {
    expect(sanitizeCss('.a { width: expression(alert(1)) }')).toBeNull()
    expect(sanitizeCss('.a { background: url(javascript:alert(1)) }')).toBeNull()
    expect(sanitizeCss('.a { -moz-binding: url(x.xml#b) }')).toBeNull()
  })

  it('无 url() 的普通 CSS 放行', () => {
    expect(sanitizeCss('.lace { border-image: linear-gradient(#fbb, #fdf) 1 }')).not.toBeNull()
  })
})

describe('injectPluginStyle · 体积上限', () => {
  beforeEach(() => {
    apiGet.mockReset()
  })

  it('超过 64KB 上限 → 拒绝注入（不建 <style>）', async () => {
    const huge = `.a { color: #fff; /* ${'x'.repeat(MAX_CLIENT_STYLE_BYTES)} */ }`
    apiGet.mockResolvedValue({ data: huge })
    const ok = await injectPluginStyle({ pluginId: 'p', id: 'big', path: '/x.css' })
    expect(ok).toBe(false)
    expect(document.querySelector('style[data-plugin-style="p:big"]')).toBeNull()
  })

  it('限额内合法 CSS → 注入成功（上限边界对侧）', async () => {
    apiGet.mockResolvedValue({ data: '.lace { color: #fbb }' })
    const ok = await injectPluginStyle({ pluginId: 'p2', id: 'ok', path: '/ok.css' })
    expect(ok).toBe(true)
    expect(document.querySelector('style[data-plugin-style="p2:ok"]')).not.toBeNull()
  })
})
