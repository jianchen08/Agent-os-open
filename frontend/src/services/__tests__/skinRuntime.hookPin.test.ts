/** @feature FP-T12 皮肤 hooks 启用确认+指纹 pin | @ci: frontend-test */
/**
 * skinRuntime hooks 消费点 pin + 确认流（2026-09-25 准入分级波2）
 *
 * 行为契约：
 * - decideSkinHook 纯判定：首启用/漂移 → confirm；一致 → run
 * - applyPluginSkin 首次应用：hooks 文本不执行，载荷挂 skinConsentStore
 *   （静态 CSS 层不受影响）
 * - pin 持久化（localStorage，消费点锚实际执行载荷）后重应用 → 不再挂确认
 * - SkinConsentDialog 确认 → saveSkinHookPin + 重应用；取消 → 仅清 pending
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { apiClient } from '@/services/api/client'
import { useSkinConsentStore } from '@/stores/skinConsentStore'
import {
  decideSkinHook,
  applyPluginSkin,
  loadSkinHookPins,
  saveSkinHookPin,
  MAX_SKIN_CSS_BYTES,
} from '../skinRuntime'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/services/api/extRoute', () => ({
  EXT_ROUTE: '/ext',
  extUrl: (pluginId: string, path: string) => `/ext/${pluginId}${path}`,
}))

const apiGet = apiClient.get as unknown as ReturnType<typeof vi.fn>

const theme = { pluginId: 'skin_plugin', skin: 's1', name: 'S1', base: 'dark' } as const

describe('decideSkinHook · 纯判定', () => {
  it('首启用 → confirm(first)', () => {
    expect(decideSkinHook('p:s', 'abc', {})).toEqual({
      action: 'confirm',
      hash: 'abc',
      reason: 'first',
    })
  })

  it('指纹漂移 → confirm(drift) 带旧指纹（≥2 组区分输入）', () => {
    const d = decideSkinHook('p:s', 'new', { 'p:s': 'old' })
    expect(d).toEqual({ action: 'confirm', hash: 'new', reason: 'drift', previous: 'old' })
  })

  it('指纹一致 → run', () => {
    expect(decideSkinHook('p:s', 'same', { 'p:s': 'same' })).toEqual({ action: 'run' })
  })
})

describe('pin 持久化', () => {
  beforeEach(() => {
    localStorage.removeItem('agentos_skin_hook_pins')
  })

  it('损坏 JSON → 空表（不抛错）', () => {
    localStorage.setItem('agentos_skin_hook_pins', '{bad json')
    expect(loadSkinHookPins()).toEqual({})
  })

  it('save → load 回读一致（多 scope 并存）', () => {
    saveSkinHookPin('a:1', 'h1')
    saveSkinHookPin('b:2', 'h2')
    expect(loadSkinHookPins()).toEqual({ 'a:1': 'h1', 'b:2': 'h2' })
  })
})

describe('applyPluginSkin · 确认流集成', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('agentos_skin_hook_pins')
    useSkinConsentStore.getState().setPending(null)
  })

  it('首次应用：静态 CSS 注入 + hooks 挂确认待批（不执行）', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (String(url).includes('merged.css')) return { data: '.deco { color: #fbb }' }
      return { data: 'export default { apply() {} }' }
    })
    await applyPluginSkin(theme as unknown as Parameters<typeof applyPluginSkin>[0])

    // 静态层已生效
    expect(document.querySelector('style[data-theme-style="skin-skin_plugin:s1"]')).not.toBeNull()
    // 动态层挂确认
    const pending = useSkinConsentStore.getState().pending
    expect(pending).not.toBeNull()
    expect(pending?.scope).toBe('skin_plugin:s1')
    expect(pending?.reason).toBe('first')
    expect(pending?.hash).toMatch(/^[0-9a-f]{64}$/)
  })

  it('pin 写入后重应用：不再挂确认（jsdom 无 blob import → 动态层失败仅记日志）', async () => {
    const hooksText = 'export default { apply() {} }'
    apiGet.mockImplementation(async (url: string) => {
      if (String(url).includes('merged.css')) return { data: '.deco { color: #fbb }' }
      return { data: hooksText }
    })
    // 预置 pin = 本次文本指纹（sha256 经 jsdom crypto.subtle 计算；缺席则跳过本用例）
    let hash = ''
    try {
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(hooksText))
      hash = Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('')
    } catch {
      return // 环境无 crypto.subtle：该路径由 dialog 确认测试兜底
    }
    saveSkinHookPin('skin_plugin:s1', hash)

    await applyPluginSkin(theme as unknown as Parameters<typeof applyPluginSkin>[0])
    expect(useSkinConsentStore.getState().pending).toBeNull()
    expect(loadSkinHookPins()['skin_plugin:s1']).toBe(hash)
  })

  it('merged.css 超限 → 整段拒绝（fail-closed）', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (String(url).includes('merged.css')) return { data: 'x'.repeat(MAX_SKIN_CSS_BYTES + 1) }
      return { data: '' }
    })
    await applyPluginSkin(theme as unknown as Parameters<typeof applyPluginSkin>[0])
    expect(
      document.querySelector('style[data-theme-style="skin-skin_plugin:s1"]'),
    ).toBeNull()
  })
})
