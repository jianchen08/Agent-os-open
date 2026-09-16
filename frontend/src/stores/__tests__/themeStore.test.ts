// @feature: FP-T12 themeStore 补测 | @ci: frontend-test
/** themeStore 行为测试：模式解析、主题加载分支、插件主题同步、DOM 应用与持久化 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { themeServiceWiring } from './helpers/storeTestMocks'
import type { Mock } from 'vitest'

const skinRuntime = vi.hoisted(() => ({
  applyPluginSkin: vi.fn(),
  clearPluginSkin: vi.fn(),
  isSkinTheme: vi.fn(() => false),
}))

const registry = vi.hoisted(() => ({
  isInitialized: vi.fn(() => true),
  getPluginTheme: vi.fn(() => null),
  getPluginThemes: vi.fn(() => [] as Array<Record<string, unknown>>),
}))

const storage = vi.hoisted(() => ({
  getUserTheme: vi.fn(() => null),
  getUserThemes: vi.fn(() => [] as Array<Record<string, unknown>>),
  deleteUserTheme: vi.fn(),
  getPreferences: vi.fn(() => ({
    reducedMotion: false,
    enableAnimations: true,
    enableGlassmorphism: true,
  })),
  mergeTheme: vi.fn((base: Record<string, unknown>, custom: Record<string, unknown>) => ({
    ...base,
    ...custom,
  })),
}))

vi.mock('@/services/skinRuntime', () => skinRuntime)
vi.mock('@/services/schema/ContributionRegistry', () => ({ contributionRegistry: registry }))
vi.mock('@/services/themeStorage', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return { ...actual, ThemeStorageService: storage }
})
vi.mock('@/services/themeService', async (importOriginal) => themeServiceWiring(importOriginal))

import { useThemeStore, initializeTheme } from '../themeStore'
import { applyTheme as applyThemeToDOM } from '@/services/themeService'

const PLUGIN_THEME = {
  id: 'dsh-skin-x',
  name: '皮肤X',
  pluginId: 'dsh',
  base: 'dark',
  variables: {} as Record<string, string>,
  skin: null,
}

function flushLoad(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('themeStore', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    registry.isInitialized.mockReturnValue(true)
    registry.getPluginTheme.mockReturnValue(null)
    registry.getPluginThemes.mockReturnValue([])
    storage.getUserTheme.mockReturnValue(null)
    storage.getUserThemes.mockReturnValue([])
    storage.deleteUserTheme.mockClear()
    useThemeStore.setState({
      mode: 'dark',
      currentThemeId: 'dark',
      resolvedTheme: 'dark',
      themeConfig: null,
      activePluginTheme: null,
      pendingThemeId: null,
      availableThemes: [],
      isLoading: false,
      bubbleAiMode: 'bubble',
      bgImageActive: false,
    })
  })

  describe('模式解析与切换', () => {
    it('setMode 非 system：主题变更走 loadTheme', async () => {
      useThemeStore.getState().setMode('light')
      await flushLoad()
      expect(useThemeStore.getState().mode).toBe('light')
      expect(useThemeStore.getState().resolvedTheme).toBe('light')
      expect(useThemeStore.getState().currentThemeId).toBe('light')
      expect(useThemeStore.getState().themeConfig?.id).toContain('light')
    })

    it('setMode 同主题：只重放 applyTheme，不重新加载', () => {
      useThemeStore.setState({
        currentThemeId: 'dark',
        themeConfig: { id: 'dark', backgrounds: {} } as never,
      })
      useThemeStore.getState().setMode('dark')
      expect(applyThemeToDOM as unknown as Mock).toHaveBeenCalled()
    })

    it('setMode system：按系统偏好解析', () => {
      const original = window.matchMedia
      window.matchMedia = vi.fn().mockImplementation((q: string) => ({
        matches: q.includes('dark'),
        media: q,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })) as unknown as typeof window.matchMedia
      useThemeStore.getState().setMode('system')
      expect(useThemeStore.getState().resolvedTheme).toBe('dark')
      window.matchMedia = original
    })
  })

  describe('loadTheme 分支', () => {
    it('用户自定义主题：basedOn 预设合并且 id/name 覆写', async () => {
      storage.getUserTheme.mockReturnValue({
        id: 'my-theme',
        name: '我的主题',
        basedOn: 'dark',
        customizations: { name: 'ignored' },
      })
      await useThemeStore.getState().loadTheme('my-theme')
      const st = useThemeStore.getState()
      expect(st.currentThemeId).toBe('my-theme')
      expect(st.themeConfig?.id).toBe('my-theme')
      expect(st.themeConfig?.name).toBe('我的主题')
      expect(st.isLoading).toBe(false)
    })

    it('dsh-skin-* 残留用户主题被清除后落到插件主题分支', async () => {
      storage.getUserTheme.mockReturnValue({ id: 'dsh-skin-x', basedOn: 'dark', customizations: {} })
      registry.getPluginTheme.mockReturnValue({ ...PLUGIN_THEME })
      await useThemeStore.getState().loadTheme('dsh-skin-x')
      expect(storage.deleteUserTheme).toHaveBeenCalledWith('dsh-skin-x')
      expect(useThemeStore.getState().activePluginTheme?.id).toBe('dsh-skin-x')
    })

    it('插件主题：配置回退 base 预设，activePluginTheme 生效', async () => {
      registry.getPluginTheme.mockReturnValue({ ...PLUGIN_THEME })
      await useThemeStore.getState().loadTheme('dsh-skin-x')
      const st = useThemeStore.getState()
      expect(st.themeConfig?.id).toBe('dark') // base 预设
      expect(st.activePluginTheme?.id).toBe('dsh-skin-x')
      expect(st.isLoading).toBe(false)
    })

    it('registry 未就绪：挂起不回退 dark', async () => {
      registry.isInitialized.mockReturnValue(false)
      await useThemeStore.getState().loadTheme('dsh-skin-x')
      const st = useThemeStore.getState()
      expect(st.pendingThemeId).toBe('dsh-skin-x')
      expect(st.themeConfig).toBeNull()
      expect(st.currentThemeId).toBe('dark') // 未覆盖持久化选择
    })

    it('retryPendingTheme：就绪后重放挂起主题', async () => {
      useThemeStore.setState({ pendingThemeId: 'dsh-skin-x' })
      registry.isInitialized.mockReturnValue(false)
      await useThemeStore.getState().retryPendingTheme() // 未就绪：不动

      registry.isInitialized.mockReturnValue(true)
      registry.getPluginTheme.mockReturnValue({ ...PLUGIN_THEME })
      await useThemeStore.getState().retryPendingTheme()
      expect(useThemeStore.getState().pendingThemeId).toBeNull()
      expect(useThemeStore.getState().activePluginTheme?.id).toBe('dsh-skin-x')
    })

    it('未知主题：registry 就绪时回退 dark', async () => {
      const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
      await useThemeStore.getState().loadTheme('no-such-theme')
      const st = useThemeStore.getState()
      expect(st.currentThemeId).toBe('dark')
      expect(st.themeConfig?.id).toBe('dark')
      expect(errSpy).toHaveBeenCalled()
      errSpy.mockRestore()
    })

    it('加载过程异常：兜底 dark 且 isLoading 复位', async () => {
      const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
      storage.getUserTheme.mockImplementation(() => {
        throw new Error('storage broken')
      })
      await useThemeStore.getState().loadTheme('broken')
      expect(useThemeStore.getState().themeConfig?.id).toBe('dark')
      expect(useThemeStore.getState().isLoading).toBe(false)
      errSpy.mockRestore()
    })
  })

  describe('可用主题列表与插件主题同步', () => {
    it('updateAvailableThemes 合并 预设+插件+用户 三源', () => {
      registry.getPluginThemes.mockReturnValue([
        { id: 'dsh-skin-x', name: '皮肤X', pluginId: 'dsh', base: 'dark', description: null },
      ])
      storage.getUserThemes.mockReturnValue([{ id: 'u1', name: 'U1', basedOn: 'light' }])

      useThemeStore.getState().updateAvailableThemes()

      const list = useThemeStore.getState().availableThemes
      expect(list.some((t) => t.id === 'dark')).toBe(true) // 预设
      const plugin = list.find((t) => t.id === 'dsh-skin-x')
      expect(plugin?.pluginId).toBe('dsh')
      expect(plugin?.description).toContain('来自插件') // 无描述时回退描述
      const user = list.find((t) => t.id === 'u1')
      expect(user?.category).toBe('special')
      expect(user?.description).toContain('基于 light')
    })

    it('syncPluginThemes：当前插件主题已应用则不重载', async () => {
      registry.getPluginTheme.mockReturnValue({ ...PLUGIN_THEME })
      useThemeStore.setState({
        currentThemeId: 'dsh-skin-x',
        activePluginTheme: { ...PLUGIN_THEME },
        themeConfig: { id: 'dsh-skin-x' } as never,
      })
      ;(applyThemeToDOM as unknown as Mock).mockClear()
      useThemeStore.getState().syncPluginThemes()
      expect(applyThemeToDOM as unknown as Mock).not.toHaveBeenCalled()
    })

    it('syncPluginThemes：插件主题消失回退 base', async () => {
      registry.getPluginTheme.mockReturnValue(null)
      useThemeStore.setState({ activePluginTheme: { ...PLUGIN_THEME } })
      useThemeStore.getState().syncPluginThemes()
      await flushLoad()
      expect(useThemeStore.getState().currentThemeId).toBe('dark')
      expect(useThemeStore.getState().activePluginTheme).toBeNull()
    })

    it('syncPluginThemes：当前插件主题未应用补一次 loadTheme', async () => {
      registry.getPluginTheme.mockReturnValue({ ...PLUGIN_THEME })
      useThemeStore.setState({ currentThemeId: 'dsh-skin-x' }) // 未应用
      useThemeStore.getState().syncPluginThemes()
      await flushLoad()
      expect(useThemeStore.getState().activePluginTheme?.id).toBe('dsh-skin-x')
    })

    it('resetTheme 回 dark；refreshThemes 复用 updateAvailableThemes', () => {
      useThemeStore.setState({ currentThemeId: 'light', mode: 'light' })
      useThemeStore.getState().resetTheme()
      expect(useThemeStore.getState().currentThemeId).toBe('dark')

      useThemeStore.getState().refreshThemes()
      expect(useThemeStore.getState().availableThemes.length).toBeGreaterThan(0)
    })
  })

  describe('applyTheme：DOM 信号与插件覆盖', () => {
    it('无配置直接返回', () => {
      expect(useThemeStore.getState().applyTheme()).toBeUndefined()
    })

    it('背景图主题：挂 body 标记与 CSS 变量；皮肤激活期让位', () => {
      const cfg = {
        id: 'bg-theme',
        category: 'dark',
        backgrounds: {
          image: {
            enabled: true,
            url: 'https://example.com/bg.png',
            position: 'center',
            size: 'cover',
            attachment: 'fixed',
            overlay: '#000',
            overlayOpacity: 0.4,
          },
          texture: { type: 'dots', color: 'rgba(0,0,0,0.2)', size: '32px', opacity: 0.2 },
        },
      } as never
      useThemeStore.setState({ themeConfig: cfg })
      useThemeStore.getState().applyTheme()

      const root = document.documentElement
      expect(document.body.classList.contains('has-bg-image')).toBe(true)
      expect(root.style.getPropertyValue('--bg-image')).toContain('bg.png')
      expect(root.style.getPropertyValue('--bg-texture')).toContain('radial-gradient')
      expect(useThemeStore.getState().bgImageActive).toBe(true)

      // 皮肤激活：主题管线不覆盖背景图信号
      useThemeStore.setState({ activePluginTheme: { ...PLUGIN_THEME, skin: {} } as never })
      useThemeStore.getState().applyTheme()
      expect(useThemeStore.getState().bgImageActive).toBe(true)
      expect(document.body.classList.contains('has-bg-image')).toBe(false)
    })

    it('无背景图：摘除标记；纹理缺省渲染 none', () => {
      useThemeStore.setState({
        themeConfig: { id: 'plain', category: 'dark', backgrounds: {} } as never,
      })
      document.body.classList.add('has-bg-image')
      useThemeStore.getState().applyTheme()
      expect(document.body.classList.contains('has-bg-image')).toBe(false)
      expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('none')
    })

    it('动效偏好关闭时过渡变量归零', () => {
      storage.getPreferences.mockReturnValue({
        reducedMotion: true,
        enableAnimations: true,
        enableGlassmorphism: true,
      })
      useThemeStore.setState({
        themeConfig: { id: 'plain', category: 'dark', backgrounds: {} } as never,
      })
      useThemeStore.getState().applyTheme()
      expect(document.documentElement.style.getPropertyValue('--transition-fast')).toBe('0ms')
    })

    it('bubble 形态：插件变量与主题声明两条来源都生效；皮肤按声明注入/摘除', () => {
      const base = { id: 'plain', category: 'dark', backgrounds: {} } as never
      useThemeStore.setState({ themeConfig: base })
      useThemeStore.getState().applyTheme()
      expect(useThemeStore.getState().bubbleAiMode).toBe('bubble')
      expect(skinRuntime.clearPluginSkin).toHaveBeenCalled()

      useThemeStore.setState({
        themeConfig: { ...base, colors: { bubble: { ai_mode: 'flat' } } } as never,
      })
      useThemeStore.getState().applyTheme()
      expect(useThemeStore.getState().bubbleAiMode).toBe('flat')

      skinRuntime.isSkinTheme.mockReturnValue(true)
      useThemeStore.setState({
        activePluginTheme: {
          ...PLUGIN_THEME,
          variables: { '--bubble-ai-mode': 'bubble' },
        } as never,
      })
      useThemeStore.getState().applyTheme()
      expect(skinRuntime.applyPluginSkin).toHaveBeenCalled()
      // 插件变量 'bubble' ≠ 'flat'，但主题声明 ai_mode='flat' 仍判 flat
      expect(useThemeStore.getState().bubbleAiMode).toBe('flat')
    })
  })

  describe('持久化与启动初始化', () => {
    it('partialize 只落 mode/currentThemeId', () => {
      useThemeStore.setState({ mode: 'light', currentThemeId: 'light' })
      const raw = localStorage.getItem('theme-storage')
      expect(raw).toBeTruthy()
      const parsed = JSON.parse(raw!)
      expect(parsed.state).toEqual({ mode: 'light', currentThemeId: 'light' })
    })

    it('initializeTheme：拉动态主题→更新列表→按持久化选择加载', async () => {
      useThemeStore.setState({ mode: 'light', currentThemeId: 'light' })
      await initializeTheme()
      const st = useThemeStore.getState()
      expect(st.resolvedTheme).toBe('light')
      expect(st.themeConfig?.id).toContain('light')
      expect(st.availableThemes.length).toBeGreaterThan(0)
    })
  })
})
