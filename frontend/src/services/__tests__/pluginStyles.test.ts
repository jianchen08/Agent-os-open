/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * pluginStyles — 插件 CSS 注入（contributes.client_styles）测试
 *
 * 覆盖 task_plugin_frontend_customization.md 任务 2：
 * - sanitizeCss：危险构造整段拒绝（expression()/javascript:/外部 @import/behavior:）
 * - scopeCss：scoped 前缀 / global 原样 / at-rule 不动
 * - injectPluginStyle：fetch → 消毒 → <style> 注入（带 nonce + data-plugin 标识）
 * - removeAllPluginStyles / syncPluginStyles：以注册表为权威清理
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

import { apiClient } from '@/services/api/client'

// ── Mock 外部依赖 ──
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

import {
  sanitizeCss,
  scopeCss,
  injectPluginStyle,
  removeAllPluginStyles,
  syncPluginStyles,
} from '@/services/pluginStyles'
import type { ClientStyleDeclaration } from '@/services/schema/ContributionRegistry'

const apiGet = apiClient.get as unknown as Mock

function styleDecl(overrides: Partial<ClientStyleDeclaration> = {}): ClientStyleDeclaration {
  return {
    id: 'gold-lace-border',
    path: '/assets/border.css',
    scope: 'global',
    pluginId: 'demo_plugin',
    ...overrides,
  }
}

describe('sanitizeCss — 消毒（fail-closed）', () => {
  it('正常 CSS 通过', () => {
    expect(sanitizeCss('body { color: red; }')).toBe('body { color: red; }')
  })

  it.each([
    ['expression()', 'body { width: expression(alert(1)) }'],
    ['javascript: url', 'a { background: url(javascript:alert(1)) }'],
    ['javascript: 带空格', 'a { background: url( javascript:alert(1)) }'],
    ['vbscript:', 'a { background: url(vbscript:msgbox(1)) }'],
    ['外部 @import', '@import url("https://evil.example/x.css");'],
    ['外部 @import 无 url()', '@import "https://evil.example/x.css";'],
    ['协议相对 @import', "@import '//evil.example/x.css';"],
    ['behavior:', 'div { behavior: url(#default#time2) }'],
    ['-moz-binding:', 'div { -moz-binding: url(http://evil/x.xml#x) }'],
  ])('拒绝 %s', (_label, css) => {
    expect(sanitizeCss(css)).toBeNull()
  })

  it('相对路径 @import（同源）放行', () => {
    const css = "@import './base.css'; body { color: red; }"
    expect(sanitizeCss(css)).toBe(css)
  })
})

describe('scopeCss — 作用域包装', () => {
  it('global（缺省）原样返回', () => {
    const css = 'body { color: red }'
    expect(scopeCss(css, 'p', 'global')).toBe(css)
    expect(scopeCss(css, 'p', undefined)).toBe(css)
  })

  it('scoped：顶层规则加 [data-plugin] 前缀', () => {
    const out = scopeCss('.a { color: red }\n.b, .c { color: blue }', 'demo_plugin', 'scoped')
    expect(out).toBe(
      '[data-plugin="demo_plugin"] .a { color: red }\n[data-plugin="demo_plugin"] .b, [data-plugin="demo_plugin"] .c { color: blue }',
    )
  })

  it('scoped：at-rule（@media/@keyframes）原样保留', () => {
    const css = '@media (max-width: 100px) { .a { color: red } }'
    const out = scopeCss(css, 'p', 'scoped')
    expect(out).toContain('@media (max-width: 100px)')
    expect(out).not.toContain('[data-plugin="p"]')
  })
})

describe('injectPluginStyle — 注入', () => {
  beforeEach(() => {
    document.head.innerHTML = ''
    apiGet.mockReset()
  })

  it('fetch CSS → 消毒 → 注入 <style>（带 nonce 与 data-plugin 标识）', async () => {
    apiGet.mockResolvedValue({ data: 'body::after { border: 2px solid gold }' })
    const ok = await injectPluginStyle(styleDecl())
    expect(ok).toBe(true)

    const styleEl = document.querySelector('style[data-plugin-style="demo_plugin:gold-lace-border"]')
    expect(styleEl).not.toBeNull()
    expect(styleEl?.getAttribute('data-plugin')).toBe('demo_plugin')
    expect(styleEl?.textContent).toContain('border: 2px solid gold')
    // nonce：jsdom 有 crypto.randomUUID 时注入会话 nonce
    if (typeof window !== 'undefined' && window.crypto?.randomUUID) {
      expect(styleEl?.getAttribute('nonce')).toBeTruthy()
    }
    // 请求路径拼接正确（path 前导 / 不重复）
    expect(apiGet).toHaveBeenCalledWith(
      '/ext/demo_plugin/assets/border.css',
      expect.objectContaining({ responseType: 'text' }),
    )
  })

  it('消毒拒绝 → 不注入并返回 false', async () => {
    apiGet.mockResolvedValue({ data: 'body { width: expression(alert(1)) }' })
    const ok = await injectPluginStyle(styleDecl())
    expect(ok).toBe(false)
    expect(document.querySelector('style[data-plugin-style]')).toBeNull()
  })

  it('fetch 失败 → 不注入并返回 false（不向上抛）', async () => {
    apiGet.mockRejectedValue(new Error('404'))
    const ok = await injectPluginStyle(styleDecl())
    expect(ok).toBe(false)
  })

  it('重复注入幂等：同 id 已存在时跳过（不发第二次 fetch）', async () => {
    apiGet.mockResolvedValue({ data: 'body { color: red }' })
    await injectPluginStyle(styleDecl())
    await injectPluginStyle(styleDecl())
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(document.querySelectorAll('style[data-plugin-style]')).toHaveLength(1)
  })
})

describe('remove / sync — 清理与同步', () => {
  beforeEach(() => {
    document.head.innerHTML = ''
    apiGet.mockReset()
    apiGet.mockResolvedValue({ data: 'body { color: red }' })
  })

  async function injectAll(styles: ClientStyleDeclaration[]): Promise<void> {
    for (const s of styles) await injectPluginStyle(s)
  }

  it('removeAllPluginStyles 清空全部插件样式', async () => {
    await injectAll([styleDecl({ id: 'a' }), styleDecl({ id: 'b', pluginId: 'other' })])
    removeAllPluginStyles()
    expect(document.querySelectorAll('style[data-plugin-style]')).toHaveLength(0)
  })

  it('syncPluginStyles：以注册表为权威——移除失效、注入新增、保留既有', async () => {
    // 已有：keep（注册表中仍存在）与 stale（已从注册表消失）
    await injectAll([styleDecl({ id: 'keep' }), styleDecl({ id: 'stale' })])
    apiGet.mockClear()

    syncPluginStyles([
      styleDecl({ id: 'keep' }),
      styleDecl({ id: 'fresh' }), // 新声明 → 应注入
    ])

    // 异步 fetch 注入完成
    await vi.waitFor(() => {
      expect(document.querySelector('style[data-plugin-style="demo_plugin:fresh"]')).not.toBeNull()
    })
    expect(document.querySelector('style[data-plugin-style="demo_plugin:keep"]')).not.toBeNull()
    // 失效样式被移除（插件禁用 → 无残留）
    expect(document.querySelector('style[data-plugin-style="demo_plugin:stale"]')).toBeNull()
    // keep 已存在 → 不重复 fetch；fresh 才发起 fetch
    const fetched = apiGet.mock.calls.map((c) => c[0] as string)
    expect(fetched).toContain('/ext/demo_plugin/assets/border.css')
  })
})
