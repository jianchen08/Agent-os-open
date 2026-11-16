/**
 * @feature: FP-T12 前端适配（UX-X8-F1/P-74/P-75） | @ci: frontend-test
 * themeStore.applyTheme 同步写首帧外观专用键（P-74/P-75 修复的行为面）
 *
 * applyTheme 是全部主题落 DOM 的单点（loadTheme 成功/回退、setMode 直通、
 * syncPluginThemes/retryPendingTheme 全部汇经此处）：每次成功应用主题即写
 * 'theme-appearance'='dark'|'light'，派生与 resolvedTheme 同源（isLightTheme），
 * 供 public/theme-init.js 首帧判定——插件/用户深色主题天然覆盖，不再依赖
 * theme-init 侧 id 白名单。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { PluginTheme, ThemeConfig } from '@/types/theme'

// applyTheme 全链路的 DOM/插件/背景管线 mock：本测试只验外观键写入这一条
// 数据流；jsdom 缺 matchMedia 的环境差也在此一并收口（桩见下）。
vi.mock('@/services/themeService', () => ({
  getPresetTheme: () => null,
  applyTheme: vi.fn(),
  applyPluginThemeVars: vi.fn(),
  clearPluginThemeVars: vi.fn(),
  derivePluginThemePreview: () => null,
  fetchDynamicThemes: vi.fn(async () => {}),
}))
vi.mock('@/services/backdropContrast', () => ({
  applyRenderedBackdropContrast: vi.fn(),
  emitBackdropTokens: vi.fn(),
  fingerprintOf: () => null,
  gatherBackdropContext: () => null,
}))
vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    getPluginTheme: () => undefined,
    getPluginThemes: () => [],
    isInitialized: () => true,
  },
}))
vi.mock('@/services/shellPlugins', () => ({
  getShellLastTheme: async () => null,
  notifyShellTheme: vi.fn(),
}))
vi.mock('@/services/skinRuntime', () => ({
  isSkinTheme: () => false,
  applyPluginSkin: vi.fn(async () => {}),
  clearPluginSkin: vi.fn(),
}))
vi.mock('@/services/themeStorage', () => ({
  ThemeStorageService: {
    getPreferences: () => ({
      reducedMotion: false,
      enableAnimations: true,
      enableGlassmorphism: true,
    }),
    getUserTheme: () => null,
    getUserThemes: () => [],
    deleteUserTheme: vi.fn(),
  },
  mergeTheme: (base: object, custom: object) => ({ ...base, ...custom }),
}))

// jsdom 未实现 matchMedia：applyMotionPreferences / system 模式解析的环境桩
window.matchMedia = ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia

const { useThemeStore } = await import('@/stores/themeStore')
const { setCacheUserId } = await import('@/utils/userScopedStorage')

/** 最小 ThemeConfig 形状：applyTheme 数据流只触 id/category/backgrounds/colors 面 */
function cfg(id: string, category: 'light' | 'dark' | 'special'): ThemeConfig {
  return { id, name: id, category, backgrounds: {}, colors: {} } as unknown as ThemeConfig
}

function pluginTheme(id: string, base: 'dark' | 'light'): PluginTheme {
  return { id, name: id, pluginId: 'test-skin-plugin', base } as unknown as PluginTheme
}

describe('applyTheme 写首帧外观专用键', () => {
  beforeEach(() => {
    localStorage.clear()
    setCacheUserId(null)
    useThemeStore.setState({ themeConfig: null, activePluginTheme: null })
  })

  it('深色配置 → ag-anon:theme-appearance = dark（纯字符串）', () => {
    useThemeStore.setState({ themeConfig: cfg('dark', 'dark') })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('dark')
  })

  it('浅色配置 → light（不误写 dark）', () => {
    useThemeStore.setState({ themeConfig: cfg('light', 'light') })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('light')
  })

  it('插件深色主题（base=dark，id 不在旧白名单）→ dark（P-75 根治面）', () => {
    useThemeStore.setState({
      themeConfig: cfg('cyber-night', 'dark'),
      activePluginTheme: pluginTheme('cyber-night', 'dark'),
    })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('dark')
  })

  it('用户动态深色主题（basedOn dark，自定义 id）→ dark', () => {
    useThemeStore.setState({ themeConfig: cfg('my-night-9', 'dark') })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('dark')
  })

  it('登录态写入本人命名空间，匿名键不可达', () => {
    setCacheUserId('u-9')
    useThemeStore.setState({ themeConfig: cfg('dark', 'dark') })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-u:u-9:theme-appearance')).toBe('dark')
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBeNull()
  })

  it('无配置早退（未成功应用主题）不写键', () => {
    useThemeStore.setState({ themeConfig: null })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBeNull()
  })

  it('性质：派生与 resolvedTheme 同源（isLightTheme 对 id 含 light 的深色 category 判 light）', () => {
    // 防拟合：字面值用例之外的一条派生性质——themeStore.isLightTheme 口径
    // id.includes('light') 即浅色，外观键与其同源不得双轨漂移
    useThemeStore.setState({ themeConfig: cfg('warm-light-v2', 'dark') })
    useThemeStore.getState().applyTheme()
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('light')
  })
})
