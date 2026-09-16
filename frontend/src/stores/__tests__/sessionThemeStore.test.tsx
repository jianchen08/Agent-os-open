/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 会话级主题 override 栈 + 作用域挂点测试（模式体系 §5.0 Wave2 件4）
 *
 * - store：载荷校验失败整包丢弃（零状态变更）；每会话独立 LIFO 栈；
 *   栈顶=当前生效档，pop 回退上一档，空栈=默认主题
 * - useSessionThemeScope：栈顶档编译成 CSS 变量应用到挂点元素子树；
 *   会话切换卸除换装；无 override/卸载不残留
 */
import { act, render } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { presetThemes } from '@/config/themes'
import { useSessionStore } from '@/stores/sessionStore'
import { getActiveSessionTheme, useSessionThemeStore } from '@/stores/sessionThemeStore'
import { compileThemeVarEntries, useSessionThemeScope } from '@/hooks/useSessionThemeScope'
import type { ThemeConfig } from '@/types/theme'

/** 两个有区分度的合法档：dark 原档 + 主色改档 */
const THEME_A: ThemeConfig = presetThemes['dark']
const THEME_B: ThemeConfig = {
  ...presetThemes['dark'],
  id: 'override-b',
  colors: { ...presetThemes['dark'].colors, primary: '#ff8800' },
}

/** 档编译后 --primary 的最终生效值（编译串含同键多次发射，后写者胜） */
function compiledPrimary(config: ThemeConfig): string {
  const hits = compileThemeVarEntries(config).filter(([k]) => k === '--primary')
  return hits.length > 0 ? hits[hits.length - 1][1] : ''
}

describe('sessionThemeStore — 会话级 override 栈', () => {
  beforeEach(() => {
    useSessionThemeStore.setState({ stacks: {} })
    useSessionStore.setState({ activeSessionId: null })
  })

  it('合法档入栈生效；二次入栈 LIFO 栈顶胜；pop 回退上一档', () => {
    expect(useSessionThemeStore.getState().pushTheme('sess-1', THEME_A)).toBe(true)
    expect(getActiveSessionTheme('sess-1')?.id).toBe(THEME_A.id)

    expect(useSessionThemeStore.getState().pushTheme('sess-1', THEME_B)).toBe(true)
    expect(getActiveSessionTheme('sess-1')?.id).toBe(THEME_B.id)

    useSessionThemeStore.getState().popTheme('sess-1')
    expect(getActiveSessionTheme('sess-1')?.id).toBe(THEME_A.id)

    useSessionThemeStore.getState().popTheme('sess-1')
    expect(getActiveSessionTheme('sess-1')).toBeUndefined()
    // 空栈再 pop = 无操作
    expect(() => useSessionThemeStore.getState().popTheme('sess-1')).not.toThrow()
  })

  it('载荷校验失败整包丢弃：返回 false 且栈零变更（缺 colors / 非对象 / 缺 components）', () => {
    const invalidPayloads: unknown[] = [
      null,
      'not-an-object',
      { id: 'x', name: 'x' }, // 缺 colors/components/effects/backgrounds
      { id: 'x', name: 'x', colors: { primary: '#fff' }, effects: {}, backgrounds: {} }, // 缺 components
    ]
    for (const payload of invalidPayloads) {
      expect(useSessionThemeStore.getState().pushTheme('sess-1', payload)).toBe(false)
    }
    expect(useSessionThemeStore.getState().stacks).toEqual({})
    expect(getActiveSessionTheme('sess-1')).toBeUndefined()
  })

  it('会话隔离：A 会话的 override 不影响 B 会话；无会话 id 拒绝', () => {
    expect(useSessionThemeStore.getState().pushTheme('sess-1', THEME_A)).toBe(true)
    expect(getActiveSessionTheme('sess-1')).toBeDefined()
    expect(getActiveSessionTheme('sess-2')).toBeUndefined()
    expect(getActiveSessionTheme(null)).toBeUndefined()
    expect(useSessionThemeStore.getState().pushTheme('', THEME_A)).toBe(false)
  })
})

describe('useSessionThemeScope — 作用域挂点', () => {
  beforeEach(() => {
    useSessionThemeStore.setState({ stacks: {} })
    useSessionStore.setState({ activeSessionId: null })
  })

  function ScopeHost(): React.ReactElement {
    const ref = useSessionThemeScope<HTMLDivElement>()
    return <div ref={ref} data-testid="scope-root" />
  }

  it('活跃会话的栈顶档编译应用到挂点（主色变量生效）；切会话卸除回默认', () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    useSessionThemeStore.getState().pushTheme('sess-1', THEME_B)

    const { getByTestId, unmount } = render(<ScopeHost />)
    const root = getByTestId('scope-root')
    expect(root.style.getPropertyValue('--primary')).toBe(compiledPrimary(THEME_B))

    // 切到无 override 的会话 → 变量卸除（回默认，由 :root 全局主题接管）
    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-2' })
    })
    expect(root.style.getPropertyValue('--primary')).toBe('')

    unmount()
  })

  it('挂点渲染时 override 已存在 → 直接生效；pop 后卸除', () => {
    const { getByTestId } = render(<ScopeHost />)
    const root = getByTestId('scope-root')
    expect(root.style.getPropertyValue('--primary')).toBe('')

    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-9' })
      useSessionThemeStore.getState().pushTheme('sess-9', THEME_A)
    })
    // dark 档主色变量生效（值为档编译结果而非空串）
    expect(root.style.getPropertyValue('--primary')).toBe(compiledPrimary(THEME_A))
    expect(compiledPrimary(THEME_A)).not.toBe('')

    act(() => {
      useSessionThemeStore.getState().popTheme('sess-9')
    })
    expect(root.style.getPropertyValue('--primary')).toBe('')
  })
})

describe('compileThemeVarEntries — 变量编译', () => {
  it('完整档编译出非空变量集合；键均为 -- 前缀且值非空', () => {
    const entries = compileThemeVarEntries(THEME_A)
    expect(entries.length).toBeGreaterThan(10)
    for (const [key, value] of entries) {
      expect(key.startsWith('--')).toBe(true)
      expect(value).not.toBe('')
    }
  })
})
