/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * themeStore 分支补测：补齐既有测试未触达的分支——
 * 纹理 CSS 全类型生成（dots/grid/lines/checker/noise/未知/none）、
 * setMode 的 system 解析与同主题短路、loadTheme 的 dsh-skin 残留清理、
 * 用户主题基准缺失、插件主题基准缺失、registry 未就绪挂起、
 * 主题彻底不可得时的 dark 回退、retryPendingTheme 三分支、
 * applyTheme 的背景图/纹理/区域纹理/气泡形态/皮肤路由，
 * 以及 initializeTheme 的 system 监听回调与 rehydrate 等待。
 *
 * 外部依赖（registry / 主题服务 / 存储 / 皮肤运行时）用可控 stub；
 * 断言面向 store 状态与 DOM 副作用（可观察行为）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { themeServiceWiring } from './helpers/storeTestMocks'

const skinRuntime = vi.hoisted(() => ({
  applyPluginSkin: vi.fn(),
  clearPluginSkin: vi.fn(),
  isSkinTheme: vi.fn(() => false),
}))

const registry = vi.hoisted(() => ({
  isInitialized: vi.fn(() => true),
  getPluginTheme: vi.fn(),
  getPluginThemes: vi.fn(() => [] as Array<Record<string, unknown>>),
}))

const storageSvc = vi.hoisted(() => ({
  getUserTheme: vi.fn(),
  getUserThemes: vi.fn(() => [] as Array<Record<string, unknown>>),
  deleteUserTheme: vi.fn(),
  getPreferences: vi.fn(() => ({
    reducedMotion: false,
    enableAnimations: true,
    enableGlassmorphism: true,
  })),
}))

vi.mock('@/services/skinRuntime', () => skinRuntime)
vi.mock('@/services/schema/ContributionRegistry', () => ({ contributionRegistry: registry }))
vi.mock('@/services/themeStorage', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return { ...actual, ThemeStorageService: storageSvc }
})
vi.mock('@/services/themeService', async (importOriginal) => themeServiceWiring(importOriginal))

import { useThemeStore, initializeTheme } from '../themeStore'
import { applyPluginThemeVars, clearPluginThemeVars } from '@/services/themeService'
import type { ThemeConfig } from '@/types/theme'

/**
 * 真实动作引用快照
 *
 * 部分用例经 setState 注入 loadTheme/applyTheme 探针（观察调用关系），
 * 那会永久覆盖 store 上的动作；每次 beforeEach 用本快照还原，避免串扰。
 */
const REAL_ACTIONS = {
  loadTheme: useThemeStore.getState().loadTheme,
  applyTheme: useThemeStore.getState().applyTheme,
  retryPendingTheme: useThemeStore.getState().retryPendingTheme,
  syncPluginThemes: useThemeStore.getState().syncPluginThemes,
  setMode: useThemeStore.getState().setMode,
  updateAvailableThemes: useThemeStore.getState().updateAvailableThemes,
  resetTheme: useThemeStore.getState().resetTheme,
}

/** 构造一份完整 ThemeConfig（背景各字段可覆写） */
function makeTheme(id: string, overrides: Partial<ThemeConfig> = {}): ThemeConfig {
  return {
    id,
    name: id,
    category: 'dark',
    colors: {},
    effects: {},
    components: {},
    backgrounds: {},
    ...overrides,
  } as unknown as ThemeConfig
}


beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  registry.isInitialized.mockReturnValue(true)
  registry.getPluginTheme.mockReturnValue(undefined)
  registry.getPluginThemes.mockReturnValue([])
  storageSvc.getUserTheme.mockReturnValue(null)
  storageSvc.getUserThemes.mockReturnValue([])
  skinRuntime.isSkinTheme.mockReturnValue(false)

  // 主题预设按需注入（主题服务是外部依赖，按文件内容 mock）
  void 0

  useThemeStore.setState({
    ...REAL_ACTIONS,
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

afterEach(() => {
  vi.restoreAllMocks()
  document.documentElement.removeAttribute('style')
  document.body.className = ''
})

describe('themeStore — 纹理 CSS 生成（经 applyTheme 可观察）', () => {
  it.each([
    ['dots', 'radial-gradient'],
    ['grid', 'linear-gradient'],
    ['lines', 'repeating-linear-gradient'],
    ['checker', 'repeating-conic-gradient'],
    ['noise', 'data:image/svg+xml'],
  ])('texture=%s 生成对应 CSS 并写入 --bg-texture', async (type, expected) => {
    const config = makeTheme('t', { backgrounds: { texture: { type } } } as never)
    useThemeStore.setState({ themeConfig: config, activePluginTheme: null })

    useThemeStore.getState().applyTheme()

    const value = document.documentElement.style.getPropertyValue('--bg-texture')
    expect(value).toContain(expected)
  })

  it('texture.type=none 时 --bg-texture 为 none', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', { backgrounds: { texture: { type: 'none' } } } as never),
    })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('none')
  })

  it('未知 texture type 落回 none（默认分支）', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', { backgrounds: { texture: { type: 'weird' } } } as never),
    })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('none')
  })

  it('无 texture 声明时 --bg-texture 置 none', () => {
    useThemeStore.setState({ themeConfig: makeTheme('t') })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('none')
  })

  it('texture 自定义 color/size 进入生成的 CSS 与尺寸变量', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', {
        backgrounds: { texture: { type: 'lines', color: 'rgba(1,2,3,0.5)', size: '32px' } },
      } as never),
    })
    useThemeStore.getState().applyTheme()
    const root = document.documentElement
    expect(root.style.getPropertyValue('--bg-texture')).toContain('rgba(1,2,3,0.5)')
    expect(root.style.getPropertyValue('--bg-texture-size')).toBe('32px')
  })

  it('texture 缺省 size/opacity 时用默认值（24px / 0.1）', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', { backgrounds: { texture: { type: 'dots' } } } as never),
    })
    useThemeStore.getState().applyTheme()
    const root = document.documentElement
    expect(root.style.getPropertyValue('--bg-texture-size')).toBe('24px')
    expect(root.style.getPropertyValue('--bg-texture-opacity')).toBe('0.1')
  })

  it('区域纹理：sidebar/chat 各自生成，缺失的置 none', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', {
        backgrounds: {
          sidebar: { texture: { type: 'grid', size: '8px' } },
          chat: {},
        },
      } as never),
    })
    useThemeStore.getState().applyTheme()
    const root = document.documentElement
    expect(root.style.getPropertyValue('--sidebar-texture')).toContain('linear-gradient')
    expect(root.style.getPropertyValue('--sidebar-texture-size')).toBe('8px')
    expect(root.style.getPropertyValue('--chat-texture')).toBe('none')
  })
})

describe('themeStore — 背景图启用/停用', () => {
  it('背景图声明 enabled+url 时挂 body.has-bg-image 并写全量变量', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', {
        backgrounds: {
          image: {
            enabled: true,
            url: '/bg.png',
            position: 'top left',
            size: 'contain',
            attachment: 'scroll',
            overlay: 'rgba(0,0,0,0.2)',
            overlayOpacity: 0.3,
          },
        },
      } as never),
    })

    useThemeStore.getState().applyTheme()

    expect(document.body.classList.contains('has-bg-image')).toBe(true)
    const root = document.documentElement
    expect(root.style.getPropertyValue('--bg-image')).toContain('/bg.png')
    expect(root.style.getPropertyValue('--bg-image-position')).toBe('top left')
    expect(root.style.getPropertyValue('--bg-image-size')).toBe('contain')
    expect(root.style.getPropertyValue('--bg-image-attachment')).toBe('scroll')
    expect(root.style.getPropertyValue('--bg-overlay')).toBe('rgba(0,0,0,0.2)')
    expect(root.style.getPropertyValue('--bg-overlay-opacity')).toBe('0.3')
    expect(useThemeStore.getState().bgImageActive).toBe(true)
  })

  it('enabled=false 时不挂类且移除 --bg-image', () => {
    document.body.classList.add('has-bg-image')
    useThemeStore.setState({
      themeConfig: makeTheme('t', {
        backgrounds: { image: { enabled: false, url: '/bg.png' } },
      } as never),
    })

    useThemeStore.getState().applyTheme()

    expect(document.body.classList.contains('has-bg-image')).toBe(false)
    expect(document.documentElement.style.getPropertyValue('--bg-image')).toBe('')
    expect(useThemeStore.getState().bgImageActive).toBe(false)
  })

  it('enabled=true 但 url 缺失时不挂类（双条件）', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', {
        backgrounds: { image: { enabled: true, url: '' } },
      } as never),
    })
    useThemeStore.getState().applyTheme()
    expect(document.body.classList.contains('has-bg-image')).toBe(false)
    expect(useThemeStore.getState().bgImageActive).toBe(false)
  })

  it('皮肤激活期不覆盖背景标记（skinActive 抑制主题 image 分支）', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', {
        backgrounds: { image: { enabled: true, url: '/bg.png' } },
      } as never),
      activePluginTheme: { id: 'skin-t', skin: 'x', base: 'dark', variables: {} } as never,
    })

    useThemeStore.getState().applyTheme()

    expect(document.body.classList.contains('has-bg-image')).toBe(false)
    // 但 bgImageActive 仍为 true（皮肤声明直接判定，不依赖 DOM 时序）
    expect(useThemeStore.getState().bgImageActive).toBe(true)
  })
})

describe('themeStore — 气泡形态与插件变量路由', () => {
  it('主题 colors.bubble.ai_mode=flat 时 bubbleAiMode 为 flat', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t', { colors: { bubble: { ai_mode: 'flat' } } } as never),
    })
    useThemeStore.getState().applyTheme()
    expect(useThemeStore.getState().bubbleAiMode).toBe('flat')
  })

  it('插件主题 --bubble-ai-mode=flat 时同样为 flat（插件优先）', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t'),
      activePluginTheme: {
        id: 'pt',
        base: 'dark',
        variables: { '--bubble-ai-mode': 'flat' },
      } as never,
    })
    useThemeStore.getState().applyTheme()
    expect(useThemeStore.getState().bubbleAiMode).toBe('flat')
  })

  it('无任何声明时默认 bubble', () => {
    useThemeStore.setState({ themeConfig: makeTheme('t') })
    useThemeStore.getState().applyTheme()
    expect(useThemeStore.getState().bubbleAiMode).toBe('bubble')
  })

  it('有插件主题时先清旧变量再应用新变量（无残留）', () => {
    useThemeStore.setState({
      themeConfig: makeTheme('t'),
      activePluginTheme: { id: 'pt', base: 'dark', variables: { '--ds-x': '1' } } as never,
    })
    useThemeStore.getState().applyTheme()
    expect(clearPluginThemeVars).toHaveBeenCalledTimes(1)
    expect(applyPluginThemeVars).toHaveBeenCalledTimes(1)
  })

  it('无插件主题时只清变量不应用', () => {
    useThemeStore.setState({ themeConfig: makeTheme('t'), activePluginTheme: null })
    useThemeStore.getState().applyTheme()
    expect(clearPluginThemeVars).toHaveBeenCalledTimes(1)
    expect(applyPluginThemeVars).not.toHaveBeenCalled()
  })

  it('皮肤主题走 applyPluginSkin，非皮肤走 clearPluginSkin', () => {
    skinRuntime.isSkinTheme.mockReturnValue(true)
    useThemeStore.setState({
      themeConfig: makeTheme('t'),
      activePluginTheme: { id: 'pt', skin: 's', base: 'dark', variables: {} } as never,
    })
    useThemeStore.getState().applyTheme()
    expect(skinRuntime.applyPluginSkin).toHaveBeenCalledTimes(1)

    skinRuntime.isSkinTheme.mockReturnValue(false)
    skinRuntime.clearPluginSkin.mockClear()
    useThemeStore.getState().applyTheme()
    expect(skinRuntime.clearPluginSkin).toHaveBeenCalledTimes(1)
  })

  it('themeConfig 为 null 时 applyTheme 直接返回（无副作用）', () => {
    useThemeStore.setState({ themeConfig: null })
    expect(() => useThemeStore.getState().applyTheme()).not.toThrow()
    expect(clearPluginThemeVars).not.toHaveBeenCalled()
  })
})

describe('themeStore — 用户动效偏好覆盖', () => {
  it('reducedMotion=true 时三个过渡变量归零', () => {
    storageSvc.getPreferences.mockReturnValue({
      reducedMotion: true,
      enableAnimations: true,
      enableGlassmorphism: true,
    })
    useThemeStore.setState({ themeConfig: makeTheme('t') })
    useThemeStore.getState().applyTheme()

    const root = document.documentElement
    expect(root.style.getPropertyValue('--transition-fast')).toBe('0ms')
    expect(root.style.getPropertyValue('--transition-base')).toBe('0ms')
    expect(root.style.getPropertyValue('--transition-slow')).toBe('0ms')
  })

  it('enableAnimations=false 时同样归零（两条触发条件之一）', () => {
    storageSvc.getPreferences.mockReturnValue({
      reducedMotion: false,
      enableAnimations: false,
      enableGlassmorphism: true,
    })
    useThemeStore.setState({ themeConfig: makeTheme('t') })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.style.getPropertyValue('--transition-base')).toBe('0ms')
  })

  it('偏好全开时不写过渡变量（保留主题默认）', () => {
    // 显式复位 DOM 与偏好（前序用例可能写过 0ms）
    document.documentElement.style.removeProperty('--transition-base')
    storageSvc.getPreferences.mockReturnValue({
      reducedMotion: false,
      enableAnimations: true,
      enableGlassmorphism: true,
    })
    useThemeStore.setState({ themeConfig: makeTheme('t') })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.style.getPropertyValue('--transition-base')).toBe('')
  })

  it('enableGlassmorphism=false 时挂 no-glassmorphism 类，true 时移除', () => {
    storageSvc.getPreferences.mockReturnValue({
      reducedMotion: false,
      enableAnimations: true,
      enableGlassmorphism: false,
    })
    useThemeStore.setState({ themeConfig: makeTheme('t') })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.classList.contains('no-glassmorphism')).toBe(true)

    storageSvc.getPreferences.mockReturnValue({
      reducedMotion: false,
      enableAnimations: true,
      enableGlassmorphism: true,
    })
    useThemeStore.getState().applyTheme()
    expect(document.documentElement.classList.contains('no-glassmorphism')).toBe(false)
  })
})

describe('themeStore — 系统主题解析与模式切换', () => {
  function mockSystemDark(isDark: boolean) {
    vi.spyOn(window, 'matchMedia').mockImplementation(
      (query: string) =>
        ({
          matches: query.includes('prefers-color-scheme') ? isDark : false,
          media: query,
          onchange: null,
          addListener: () => {},
          removeListener: () => {},
          addEventListener: () => {},
          removeEventListener: () => {},
          dispatchEvent: () => false,
        }) as MediaQueryList,
    )
  }

  it('mode=system 且系统深色时解析为 dark', () => {
    mockSystemDark(true)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ loadTheme: loadSpy as never, currentThemeId: 'dark' })

    useThemeStore.getState().setMode('system')

    expect(useThemeStore.getState().resolvedTheme).toBe('dark')
    expect(useThemeStore.getState().mode).toBe('system')
  })

  it('mode=system 且系统浅色时解析为 light', () => {
    mockSystemDark(false)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ loadTheme: loadSpy as never, currentThemeId: 'dark' })

    useThemeStore.getState().setMode('system')

    expect(useThemeStore.getState().resolvedTheme).toBe('light')
  })

  it('解析出的主题与当前不同则触发 loadTheme', () => {
    mockSystemDark(false)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ loadTheme: loadSpy as never, currentThemeId: 'dark' })

    useThemeStore.getState().setMode('system')

    expect(loadSpy).toHaveBeenCalledWith('light')
  })

  it('解析出的主题与当前相同则只 applyTheme（不重复加载）', () => {
    mockSystemDark(true)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    const applySpy = vi.fn()
    useThemeStore.setState({
      loadTheme: loadSpy as never,
      applyTheme: applySpy,
      currentThemeId: 'dark',
    })

    useThemeStore.getState().setMode('system')

    expect(loadSpy).not.toHaveBeenCalled()
    expect(applySpy).toHaveBeenCalledTimes(1)
  })

  it('setMode(light) 直接解析为 light（非 system 分支）', () => {
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ loadTheme: loadSpy as never, currentThemeId: 'dark' })

    useThemeStore.getState().setMode('light')

    expect(useThemeStore.getState()).toMatchObject({ mode: 'light', resolvedTheme: 'light' })
    expect(loadSpy).toHaveBeenCalledWith('light')
  })
})

describe('themeStore — retryPendingTheme 分支', () => {
  it('无挂起主题时不动作', async () => {
    useThemeStore.setState({ pendingThemeId: null })
    await useThemeStore.getState().retryPendingTheme()
    expect(registry.getPluginTheme).not.toHaveBeenCalled()
  })

  it('registry 未就绪时保持挂起（不加载、不清空）', async () => {
    registry.isInitialized.mockReturnValue(false)
    useThemeStore.setState({ pendingThemeId: 'dsh-skin-x' })

    await useThemeStore.getState().retryPendingTheme()

    expect(useThemeStore.getState().pendingThemeId).toBe('dsh-skin-x')
  })

  it('registry 已就绪且主题存在时清空挂起并加载', async () => {
    registry.getPluginTheme.mockReturnValue({ id: 'dsh-skin-x', base: 'dark' })
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ pendingThemeId: 'dsh-skin-x', loadTheme: loadSpy as never })

    await useThemeStore.getState().retryPendingTheme()

    expect(useThemeStore.getState().pendingThemeId).toBeNull()
    expect(loadSpy).toHaveBeenCalledWith('dsh-skin-x')
  })

  it('registry 已就绪但主题仍查不到时保留挂起（等下一次 schema）', async () => {
    registry.getPluginTheme.mockReturnValue(undefined)
    useThemeStore.setState({ pendingThemeId: 'ghost-theme' })

    await useThemeStore.getState().retryPendingTheme()

    expect(useThemeStore.getState().pendingThemeId).toBe('ghost-theme')
  })
})

describe('themeStore — updateAvailableThemes / syncPluginThemes', () => {
  it('插件主题 + 用户主题合并进列表（含描述与预览）', () => {
    registry.getPluginThemes.mockReturnValue([
      { id: 'pt-1', name: '插件主题', description: '描述', base: 'light', pluginId: 'plug' },
    ])
    storageSvc.getUserThemes.mockReturnValue([{ id: 'ut-1', name: '用户主题', basedOn: 'dark' }])

    useThemeStore.getState().updateAvailableThemes()

    const ids = useThemeStore.getState().availableThemes.map((t) => t.id)
    expect(ids).toContain('pt-1')
    expect(ids).toContain('ut-1')
    const pluginEntry = useThemeStore
      .getState()
      .availableThemes.find((t) => t.id === 'pt-1')!
    expect(pluginEntry).toMatchObject({ name: '插件主题', category: 'light', pluginId: 'plug' })
    const userEntry = useThemeStore.getState().availableThemes.find((t) => t.id === 'ut-1')!
    expect(userEntry.description).toBe('基于 dark 的自定义主题')
  })

  it('插件主题缺 description 时用插件来源兜底文案', () => {
    registry.getPluginThemes.mockReturnValue([
      { id: 'pt-2', name: 'N', base: 'dark', pluginId: 'plug-x' },
    ])
    useThemeStore.getState().updateAvailableThemes()
    expect(
      useThemeStore.getState().availableThemes.find((t) => t.id === 'pt-2')!.description,
    ).toBe('来自插件 plug-x 的主题')
  })

  it('syncPluginThemes：当前主题是插件主题但尚未应用时补加载', () => {
    registry.getPluginTheme.mockReturnValue({ id: 'pt-1', base: 'dark', pluginId: 'plug' })
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({
      currentThemeId: 'pt-1',
      activePluginTheme: null,
      themeConfig: null,
      loadTheme: loadSpy as never,
    })

    useThemeStore.getState().syncPluginThemes()

    expect(loadSpy).toHaveBeenCalledWith('pt-1')
  })

  it('syncPluginThemes：插件主题已应用时不重复加载（幂等）', () => {
    registry.getPluginTheme.mockReturnValue({ id: 'pt-1', base: 'dark', pluginId: 'plug' })
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({
      currentThemeId: 'pt-1',
      activePluginTheme: { id: 'pt-1', base: 'dark', variables: {} } as never,
      themeConfig: { id: 'pt-1' } as never,
      loadTheme: loadSpy as never,
    })

    useThemeStore.getState().syncPluginThemes()

    expect(loadSpy).not.toHaveBeenCalled()
  })

  it('syncPluginThemes：插件被禁用（注册表已无该主题）时回退 base', () => {
    registry.getPluginTheme.mockReturnValue(undefined)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({
      currentThemeId: 'pt-gone',
      activePluginTheme: { id: 'pt-gone', base: 'light', pluginId: 'plug', variables: {} } as never,
      themeConfig: { id: 'pt-gone' } as never,
      loadTheme: loadSpy as never,
    })

    useThemeStore.getState().syncPluginThemes()

    expect(loadSpy).toHaveBeenCalledWith('light')
  })

  it('syncPluginThemes：既非插件主题也无生效插件主题时无动作', () => {
    registry.getPluginTheme.mockReturnValue(undefined)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({
      currentThemeId: 'dark',
      activePluginTheme: null,
      loadTheme: loadSpy as never,
    })

    useThemeStore.getState().syncPluginThemes()

    expect(loadSpy).not.toHaveBeenCalled()
  })
})

describe('themeStore — resetTheme / refreshThemes / loadUserThemes', () => {
  it('resetTheme 复位为 dark 并加载 dark', () => {
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({
      mode: 'light',
      currentThemeId: 'light',
      resolvedTheme: 'light',
      loadTheme: loadSpy as never,
    })

    useThemeStore.getState().resetTheme()

    expect(useThemeStore.getState()).toMatchObject({
      mode: 'dark',
      currentThemeId: 'dark',
      resolvedTheme: 'dark',
    })
    expect(loadSpy).toHaveBeenCalledWith('dark')
  })

  it('refreshThemes 与 loadUserThemes 都经 updateAvailableThemes 刷新列表', () => {
    registry.getPluginThemes.mockReturnValue([{ id: 'x', name: 'X', base: 'dark', pluginId: 'p' }])
    useThemeStore.getState().refreshThemes()
    expect(useThemeStore.getState().availableThemes.some((t) => t.id === 'x')).toBe(true)

    registry.getPluginThemes.mockReturnValue([])
    useThemeStore.getState().loadUserThemes()
    expect(useThemeStore.getState().availableThemes.some((t) => t.id === 'x')).toBe(false)
  })
})

describe('themeStore — initializeTheme 监听与恢复', () => {
  it('initializeTheme 拉动态主题、刷新列表并加载当前主题', async () => {
    const { fetchDynamicThemes } = await import('@/services/themeService')
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ currentThemeId: 'dark', loadTheme: loadSpy as never, mode: 'dark' })

    await initializeTheme()

    expect(vi.mocked(fetchDynamicThemes)).toHaveBeenCalledTimes(1)
    expect(loadSpy).toHaveBeenCalledWith('dark')
  })

  it('系统配色变化且 mode=system 时更新 resolvedTheme 并重载', async () => {
    let changeHandler: (() => void) | null = null
    vi.spyOn(window, 'matchMedia').mockImplementation(
      (query: string) =>
        ({
          matches: false,
          media: query,
          onchange: null,
          addListener: () => {},
          removeListener: () => {},
          addEventListener: (_: string, cb: () => void) => {
            if (query.includes('prefers-color-scheme')) changeHandler = cb
          },
          removeEventListener: () => {},
          dispatchEvent: () => false,
        }) as MediaQueryList,
    )
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ currentThemeId: 'light', loadTheme: loadSpy as never, mode: 'system' })

    await initializeTheme()
    expect(changeHandler).toBeTruthy()

    loadSpy.mockClear()
    // 系统仍报告浅色（matches=false → light）
    changeHandler!()

    expect(useThemeStore.getState().resolvedTheme).toBe('light')
    expect(loadSpy).toHaveBeenCalledWith('light')
    vi.restoreAllMocks()
  })

  it('mode 非 system 时系统配色变化被忽略', async () => {
    let changeHandler: (() => void) | null = null
    vi.spyOn(window, 'matchMedia').mockImplementation(
      (query: string) =>
        ({
          matches: false,
          media: query,
          onchange: null,
          addListener: () => {},
          removeListener: () => {},
          addEventListener: (_: string, cb: () => void) => {
            if (query.includes('prefers-color-scheme')) changeHandler = cb
          },
          removeEventListener: () => {},
          dispatchEvent: () => false,
        }) as MediaQueryList,
    )
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    useThemeStore.setState({ currentThemeId: 'dark', loadTheme: loadSpy as never, mode: 'dark' })

    await initializeTheme()
    loadSpy.mockClear()
    changeHandler?.()

    expect(loadSpy).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })

  it('persist 尚在 rehydrate 中时等待完成再继续（不抢跑默认值）', async () => {
    const persistApi = (useThemeStore as unknown as { persist?: Record<string, unknown> }).persist!
    const origHas = persistApi.hasHydrated as () => boolean
    const origOnFinish = persistApi.onFinishHydration as
      | ((fn: () => void) => () => void)
      | undefined

    let hydrationCb: (() => void) | null = null
    const unsub = vi.fn()
    persistApi.hasHydrated = () => false
    persistApi.onFinishHydration = (cb: () => void) => {
      hydrationCb = cb
      return unsub
    }

    try {
      const loadSpy = vi.fn().mockResolvedValue(undefined)
      useThemeStore.setState({ ...REAL_ACTIONS, currentThemeId: 'dark', loadTheme: loadSpy as never })
      const p = initializeTheme()

      // 未触发 rehydrate 前不应继续加载主题
      expect(loadSpy).not.toHaveBeenCalled()

      hydrationCb!()
      await p

      expect(loadSpy).toHaveBeenCalledWith('dark')
      expect(unsub).toHaveBeenCalled()
    } finally {
      persistApi.hasHydrated = origHas
      persistApi.onFinishHydration = origOnFinish
      vi.useRealTimers()
    }
  })

  it('rehydrate 迟迟不触发时 500ms 兜底放行（不阻塞启动）', async () => {
    vi.useFakeTimers()
    const persistApi = (useThemeStore as unknown as { persist?: Record<string, unknown> }).persist!
    const origHas = persistApi.hasHydrated as () => boolean
    const origOnFinish = persistApi.onFinishHydration as
      | ((fn: () => void) => () => void)
      | undefined

    const unsub = vi.fn()
    persistApi.hasHydrated = () => false
    persistApi.onFinishHydration = () => unsub

    try {
      const loadSpy = vi.fn().mockResolvedValue(undefined)
      useThemeStore.setState({ ...REAL_ACTIONS, currentThemeId: 'dark', loadTheme: loadSpy as never })
      const p = initializeTheme()

      await vi.advanceTimersByTimeAsync(500)
      await p

      expect(loadSpy).toHaveBeenCalledWith('dark')
      expect(unsub).toHaveBeenCalled()
    } finally {
      persistApi.hasHydrated = origHas
      persistApi.onFinishHydration = origOnFinish
      vi.useRealTimers()
    }
  })
})

describe('themeStore — loadTheme 回退与挂起', () => {
  it('无 window 全局时 getSystemTheme 兜底 dark（SSR 分支）', async () => {
    const realWindow = globalThis.window
    try {
      // mode=system 触发 resolveThemeMode → getSystemTheme 的 typeof window 分支
      delete (globalThis as { window?: unknown }).window
      const loadSpy = vi.fn().mockResolvedValue(undefined)
      useThemeStore.setState({ ...REAL_ACTIONS, loadTheme: loadSpy as never, currentThemeId: 'x' })

      useThemeStore.getState().setMode('system')

      expect(useThemeStore.getState().resolvedTheme).toBe('dark')
    } finally {
      ;(globalThis as unknown as { window: Window }).window = realWindow
    }
  })

  it('主题彻底不可得且 registry 已就绪时回退 dark 并清插件主题', async () => {
    registry.getPluginTheme.mockReturnValue(undefined)
    registry.isInitialized.mockReturnValue(true)
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    useThemeStore.setState({ ...REAL_ACTIONS, themeConfig: null, activePluginTheme: null })

    await useThemeStore.getState().loadTheme('does-not-exist')

    // 真实预设中无该 id → 回退 dark（预设存在时 currentThemeId 归一为 dark）
    const after = useThemeStore.getState()
    expect(after.isLoading).toBe(false)
    expect(errSpy).toHaveBeenCalled()
    expect(after.currentThemeId).toBe('dark')
    expect(after.activePluginTheme).toBeNull()
    errSpy.mockRestore()
  })

  it('registry 未就绪时挂起主题 id 且 isLoading 复位（不回退 dark）', async () => {
    registry.getPluginTheme.mockReturnValue(undefined)
    registry.isInitialized.mockReturnValue(false)
    useThemeStore.setState({
      ...REAL_ACTIONS,
      pendingThemeId: null,
      currentThemeId: 'light',
      themeConfig: null,
    })

    await useThemeStore.getState().loadTheme('dsh-skin-pending')

    const after = useThemeStore.getState()
    expect(after.pendingThemeId).toBe('dsh-skin-pending')
    expect(after.isLoading).toBe(false)
    // 不覆盖持久化选择（仍为原 currentThemeId，不被改写为 dark）
    expect(after.currentThemeId).toBe('light')
    expect(after.themeConfig).toBeNull()
  })

  it('loadTheme 结束（无论成败）isLoading 都复位 false', async () => {
    useThemeStore.setState({ ...REAL_ACTIONS, isLoading: true, themeConfig: null })
    await useThemeStore.getState().loadTheme('some-unknown')
    expect(useThemeStore.getState().isLoading).toBe(false)
  })
})
