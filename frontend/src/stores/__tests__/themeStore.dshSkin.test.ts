/**
 * DSH 皮肤主题激活链端到端（2026-08-21 图片不显示排查的回归锁定）
 *
 * 链路：contributionRegistry.registerFromSchema（内核 schema 真实形态）
 * → themeStore.setTheme('dsh-skin-<id>') → loadTheme pluginTheme 分支
 * → applyTheme + applyPluginThemeVars → 断言 DOM 效果：
 *   - html 内联 style 含皮肤 --ds-* 令牌与 --bg-image（立绘层变量）
 *   - body.has-bg-image 类挂上（原生背景图层激活）
 *   - localStorage 曾被退役方案写入的 dsh-skin-* 用户主题被清理（截胡修复）
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { contributionRegistry } from '@/services/schema/ContributionRegistry'

const MIKU_THEME = {
  id: 'dsh-skin-miku',
  name: '初音未来 · 电子歌姬',
  description: 'DSH 皮肤',
  base: 'light',
  variables: {
    '--ds-bg-canvas': '#eef5ff',
    '--ds-accent-primary': '#2e9bff',
    '--background': '214 100% 97%',
  },
  backgrounds: {
    image: {
      enabled: true,
      url: '/ext/dsh_adapter/styles/skin-assets/miku/assets/miku-art-light.jpg',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(244, 250, 255, 0.5)',
      overlayOpacity: 1,
    },
  },
}

describe('themeStore DSH 皮肤激活链', () => {
  beforeEach(() => {
    localStorage.clear()
    document.body.className = ''
    document.documentElement.removeAttribute('style')
    contributionRegistry.clear()
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'dsh_adapter', plugin_name: 'DSH Adapter', contributes: { themes: [MIKU_THEME] } },
      ],
    })
  })

  afterEach(() => {
    localStorage.clear()
    document.body.className = ''
    document.documentElement.removeAttribute('style')
  })

  it('选择皮肤主题：插件主题命中并激活立绘背景层', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')

    const state = useThemeStore.getState()
    expect(state.activePluginTheme?.id).toBe('dsh-skin-miku')

    const htmlStyle = document.documentElement.getAttribute('style') ?? ''
    expect(htmlStyle).toContain('--ds-bg-canvas')
    expect(htmlStyle).toContain('--bg-image')
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
  })

  it('退役方案残留的 dsh-skin-* 用户主题被清理且不截胡', async () => {
    // 模拟已删除的 dshSkinTheme.ts 写入的残留
    localStorage.setItem(
      'theme_user_custom',
      JSON.stringify([
        { id: 'dsh-skin-miku', name: '旧残留', basedOn: 'dark', customizations: {}, updatedAt: '2026-08-20T00:00:00Z' },
      ]),
    )
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')

    // 残留被删 + 插件主题生效（立绘层激活证明未走残留分支）
    const stored = localStorage.getItem('theme_user_custom') ?? '[]'
    expect(JSON.parse(stored).find((t: { id: string }) => t.id === 'dsh-skin-miku')).toBeUndefined()
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
  })
})

describe('会话恢复时序（registry 未就绪挂起→注册后重放）', () => {
  it('registry 未就绪时插件主题挂起不回退，retry 后恢复', async () => {
    localStorage.clear()
    document.body.className = ''
    document.documentElement.removeAttribute('style')
    // 模拟会话启动时序：registry 未注册（reset 清空 + initialized=false）
    contributionRegistry.clear()
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku') // persist 恢复路径等价

    // registry 就绪前重新 loadTheme（刷新时序）：挂起、不回退、不覆盖选择
    await useThemeStore.getState().loadTheme('dsh-skin-miku')
    const state = useThemeStore.getState()
    expect(state.pendingThemeId).toBe('dsh-skin-miku')
    expect(state.currentThemeId).not.toBe('dark') // 未被回退覆盖

    // schema 注册（growthLoop）→ 重放
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'dsh_adapter', contributes: { themes: [MIKU_THEME] } },
      ],
    })
    await useThemeStore.getState().retryPendingTheme()
    const after = useThemeStore.getState()
    expect(after.pendingThemeId).toBeNull()
    expect(after.activePluginTheme?.id).toBe('dsh-skin-miku')
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
    expect(document.documentElement.style.getPropertyValue('--ds-bg-canvas')).toBe('#eef5ff')
  })
})
