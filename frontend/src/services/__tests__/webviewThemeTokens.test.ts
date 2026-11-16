/** @feature: FP-0.2.WebView主题令牌 | @ci: frontend-test */
/**
 * buildWebviewThemeTokens 载荷构造测试（theme.sync 下行载荷）
 *
 * 重点锁 --ag-primary 面（1017 用户实测「分界条恒紫」修复）：品牌主色入载荷
 * +浅底派生；accent 面为既有契约随迁断言（防键丢失回归）。
 */
import { describe, it, expect } from 'vitest'
import { buildWebviewThemeTokens } from '../webviewThemeTokens'
import type { ThemeConfig } from '@/types/theme'

function cfg(partial: Partial<ThemeConfig['colors']>): ThemeConfig {
  return {
    colors: {
      background: { main: '#04060F', card: '#0A1226' },
      text: { primary: '#F1F5F9', secondary: '#94A3B8' },
      border: { default: '#1E293B' },
      ...partial,
    },
  } as ThemeConfig
}

describe('buildWebviewThemeTokens primary 面', () => {
  it('声明 primary 时：--ag-primary 原值透传，--ag-primary-soft 按 0.14 半透明派生', () => {
    const tokens = buildWebviewThemeTokens(cfg({ primary: '#22D3EE', accent: '#7C3AED' }))
    expect(tokens['--ag-primary']).toBe('#22D3EE')
    expect(tokens['--ag-primary-soft']).toBe('rgba(34, 211, 238, 0.14)')
    // accent 面既有契约不回归
    expect(tokens['--ag-accent']).toBe('#7C3AED')
    expect(tokens['--ag-accent-soft']).toBe('rgba(124, 58, 237, 0.14)')
  })

  it('primary 缺省：回退宿主 --primary 变量值；不可解析则浅底回退选中底变量', () => {
    const tokens = buildWebviewThemeTokens(cfg({}))
    expect(tokens['--ag-primary']).toBe('') // jsdom 无宿主变量 → 空串（不伪造）
    expect(tokens['--ag-primary-soft']).toBe('')
  })
})
