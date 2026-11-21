/** @feature FP-RP面板v3.1刀3 | @ci: frontend-test */
/**
 * pluginStyles · client_styles modes 会话域门测试
 * （ADR 2026-10-14-message-card-body-form）
 *
 * 契约：带 modes 的声明仅当活跃会话 mode ∈ modes 时注入；会话切换（syncPluginStyles
 * 换活跃 mode 重入）按注册表权威移除门外样式（与插件禁用同路径）；无 modes 声明
 * 永远注入（既有全局装饰语义零变化）。注册面：registerFromSchema 解析 modes
 * （非法形态 = 不限域）。
 */
import { waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))
import {
  clientStyleModeGateOpen,
  removeAllPluginStyles,
  syncPluginStyles,
} from '@/services/pluginStyles'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { ClientStyleDeclaration } from '@/services/schema/ContributionRegistry'
import type { Mock } from 'vitest'

const apiGet = apiClient.get as unknown as Mock

function decl(overrides: Partial<ClientStyleDeclaration> = {}): ClientStyleDeclaration {
  return {
    id: 'rp_stream_cursor',
    path: '/styles/stream_cursor.css',
    scope: 'global',
    pluginId: 'mode_roleplay',
    ...overrides,
  }
}

function injectedKeys(): string[] {
  return Array.from(document.querySelectorAll('style[data-plugin-style]')).map((el) =>
    el.getAttribute('data-plugin-style'),
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  removeAllPluginStyles()
  contributionRegistry.clear()
  apiGet.mockResolvedValue({ data: '.md-cursor { width: 3px; }' })
})

describe('clientStyleModeGateOpen — 会话域门纯判定', () => {
  it('无 modes / 空数组 = 不限域（mode null 也开）', () => {
    expect(clientStyleModeGateOpen(decl(), null)).toBe(true)
    expect(clientStyleModeGateOpen(decl({ modes: [] }), null)).toBe(true)
    expect(clientStyleModeGateOpen(decl({ modes: [] }), 'roleplay')).toBe(true)
  })

  it('带 modes：活跃 mode 在域内开、域外/null 关（两组有区分度）', () => {
    const gated = decl({ modes: ['roleplay'] })
    expect(clientStyleModeGateOpen(gated, 'roleplay')).toBe(true)
    expect(clientStyleModeGateOpen(gated, 'coding')).toBe(false)
    expect(clientStyleModeGateOpen(gated, null)).toBe(false)
  })
})

describe('syncPluginStyles — modes 门驱动的注入/移除', () => {
  it('域内会话：带门声明注入；切到域外会话（重入 sync）→ 样式移除', async () => {
    const gated = decl({ modes: ['roleplay'] })
    syncPluginStyles([gated], 'roleplay')
    await waitFor(() => expect(injectedKeys()).toEqual(['mode_roleplay:rp_stream_cursor']))

    // 会话切换：切到无绑定会话（activeMode null）→ 门外，按权威移除
    syncPluginStyles([gated], null)
    await waitFor(() => expect(injectedKeys()).toEqual([]))
  })

  it('无门声明不受活跃 mode 影响（null/任意 mode 均注入）', async () => {
    syncPluginStyles([decl({ id: 'global_decor' })], null)
    await waitFor(() => expect(injectedKeys()).toEqual(['mode_roleplay:global_decor']))
    syncPluginStyles([decl({ id: 'global_decor' })], 'coding')
    await waitFor(() => expect(injectedKeys()).toEqual(['mode_roleplay:global_decor']))
  })

  it('带门与无门声明混挂：域切换只动带门样式（无门零变化）', async () => {
    const mixed = [decl({ id: 'global_decor' }), decl({ modes: ['roleplay'] })]
    syncPluginStyles(mixed, 'roleplay')
    await waitFor(() =>
      expect(injectedKeys().sort()).toEqual([
        'mode_roleplay:global_decor',
        'mode_roleplay:rp_stream_cursor',
      ]),
    )

    syncPluginStyles(mixed, 'coding')
    await waitFor(() => expect(injectedKeys()).toEqual(['mode_roleplay:global_decor']))
  })
})

describe('registerFromSchema — client_styles modes 解析', () => {
  it('合法 modes 收录；非法形态（空数组/含非字符串）= 不限域（键缺席）', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p_ok',
          contributes: { client_styles: [{ id: 'a', path: '/a.css', modes: ['roleplay'] }] },
        },
        {
          plugin_id: 'p_empty',
          contributes: { client_styles: [{ id: 'b', path: '/b.css', modes: [] }] },
        },
        {
          plugin_id: 'p_bad',
          contributes: { client_styles: [{ id: 'c', path: '/c.css', modes: ['roleplay', 42] }] },
        },
      ],
    })
    const styles = contributionRegistry.getClientStyles()
    expect(styles.find((s) => s.pluginId === 'p_ok')?.modes).toEqual(['roleplay'])
    expect(styles.find((s) => s.pluginId === 'p_empty')?.modes).toBeUndefined()
    expect(styles.find((s) => s.pluginId === 'p_bad')?.modes).toBeUndefined()
  })
})
