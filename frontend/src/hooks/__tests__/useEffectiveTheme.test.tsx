// @feature: FP-0.2.四 前端Schema（widget 主题数据面） | @ci: frontend-test
/**
 * useEffectiveTheme — widget 层「当前生效主题」读面测试（ADR 2026-10-10 §5）
 *
 * 三态契约：活跃会话 override 栈顶优先（override 期间插件变量不叠加，
 * pluginVars=null）；无 override 落宿主全局主题 + 插件主题变量叠加；
 * 两者皆无 → config=null（widget 自行兜底默认皮肤）。
 * 真实 store 直种（非 mock），两组区分度主题入栈。
 */
import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { presetThemes } from '@/config/themes'
import { useEffectiveTheme } from '@/hooks/useSessionThemeScope'
import { useSessionStore } from '@/stores/sessionStore'
import { useSessionThemeStore } from '@/stores/sessionThemeStore'
import { useThemeStore } from '@/stores/themeStore'
import type { PluginTheme } from '@/types/theme'

const DEEP_SPACE = presetThemes['deep-space']
const OCEAN_BREEZE = presetThemes['ocean-breeze']
const PLUGIN_THEME: PluginTheme = {
  id: 'tier-skin',
  name: 'Tier 皮肤',
  base: 'dark',
  pluginId: 'p1',
  variables: { '--ds-accent': '#ff8a5c' },
}

describe('useEffectiveTheme — 当前生效主题三态', () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: null })
    useSessionThemeStore.setState({ stacks: {} })
    useThemeStore.setState({ themeConfig: null, activePluginTheme: null })
  })

  it('会话 override 栈顶优先：config=栈顶主题，pluginVars=null（override 期间插件变量不叠加）', () => {
    useSessionStore.setState({ activeSessionId: 's1' })
    useSessionThemeStore.setState({ stacks: { s1: [DEEP_SPACE, OCEAN_BREEZE] } })
    useThemeStore.setState({ themeConfig: DEEP_SPACE, activePluginTheme: PLUGIN_THEME })
    const { result } = renderHook(() => useEffectiveTheme())
    expect(result.current.config).toBe(OCEAN_BREEZE)
    expect(result.current.pluginVars).toBeNull()
  })

  it('无 override → 宿主全局主题 + 插件主题变量叠加；插件主题缺席 → pluginVars=null', () => {
    useSessionStore.setState({ activeSessionId: 's1' })
    useThemeStore.setState({ themeConfig: DEEP_SPACE, activePluginTheme: PLUGIN_THEME })
    const withPlugin = renderHook(() => useEffectiveTheme())
    expect(withPlugin.result.current.config).toBe(DEEP_SPACE)
    expect(withPlugin.result.current.pluginVars).toEqual({ '--ds-accent': '#ff8a5c' })

    useThemeStore.setState({ activePluginTheme: null })
    const bare = renderHook(() => useEffectiveTheme())
    expect(bare.result.current.config).toBe(DEEP_SPACE)
    expect(bare.result.current.pluginVars).toBeNull()
  })

  it('无会话无全局 → config=null（widget 兜底默认皮肤）', () => {
    const { result } = renderHook(() => useEffectiveTheme())
    expect(result.current.config).toBeNull()
    expect(result.current.pluginVars).toBeNull()
  })
})
