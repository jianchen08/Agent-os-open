/**
 * 主题状态管理
 *
 * 从前端预设主题和用户自定义主题加载配置
 * 支持 light/dark/system 模式
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import { themeList } from '@/config/themes'
import {
  getPresetTheme,
  applyTheme as applyThemeToDOM,
  fetchDynamicThemes,
} from '@/services/themeService'
import { ThemeStorageService, mergeTheme } from '@/services/themeStorage'
import type { ThemeConfig, ThemeInfo, ThemeMode } from '@/types/theme'

export type { ThemeMode } from '@/types/theme'

export interface ThemeState {
  /** 当前主题模式 */
  mode: ThemeMode
  /** 当前主题 ID */
  currentThemeId: string
  /** 实际应用的主题（考虑 system 模式） */
  resolvedTheme: 'light' | 'dark'
  /** 当前加载的主题配置 */
  themeConfig: ThemeConfig | null
  /** 可用主题列表（预设 + 用户自定义） */
  availableThemes: ThemeInfo[]
  /** 是否正在加载 */
  isLoading: boolean
}

export interface ThemeActions {
  /** 设置主题模式 */
  setMode: (mode: ThemeMode) => void
  /** 切换到指定主题 */
  setTheme: (themeId: string) => Promise<void>
  /** 加载主题配置 */
  loadTheme: (themeId: string) => Promise<void>
  /** 加载用户自定义主题 */
  loadUserThemes: () => void
  /** 应用主题到 DOM */
  applyTheme: () => void
  /** 重置为默认主题 */
  resetTheme: () => void
  /** 更新可用主题列表 */
  updateAvailableThemes: () => void
  /** 刷新主题列表 */
  refreshThemes: () => void
}

/**
 * 获取系统主题偏好
 */
function getSystemTheme(): 'light' | 'dark' {
  if (typeof window === 'undefined') return 'dark'
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/**
 * 解析主题模式
 */
function resolveThemeMode(mode: ThemeMode): 'light' | 'dark' {
  if (mode === 'system') {
    return getSystemTheme()
  }
  return mode
}

/**
 * 判断主题是否为浅色主题
 */
function isLightTheme(config: ThemeConfig): boolean {
  return config.category === 'light' || config.id === 'light' || config.id.includes('light')
}

/**
 * 生成纹理 CSS
 */
function generateTextureCSS(texture: ThemeConfig['backgrounds']['texture']): string {
  if (!texture || texture.type === 'none') return 'none'

  const { type, color = 'rgba(255,255,255,0.03)', size = '24px' } = texture

  switch (type) {
    case 'dots':
      return `radial-gradient(${color} 1px, transparent 1px)`
    case 'grid':
      return `linear-gradient(${color} 1px, transparent 1px), linear-gradient(90deg, ${color} 1px, transparent 1px)`
    case 'lines':
      return `repeating-linear-gradient(0deg, ${color}, ${color} 1px, transparent 1px, transparent ${size})`
    case 'noise':
      return `url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.65' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%' height='100%' filter='url(%23noise)'/%3E%3C/svg%3E")`
    default:
      return 'none'
  }
}

/**
 * 应用用户动效偏好（ThemePreferences）覆盖主题默认 effects
 *
 * 优先级：系统 prefers-reduced-motion > 用户 reducedMotion > 用户 enableAnimations > 主题 effects
 * 任一为「减少/关闭」则强制过渡时长归零，让无障碍偏好真正生效。
 */
function applyMotionPreferences(root: HTMLElement): void {
  const prefs = ThemeStorageService.getPreferences()
  const reduceMotion =
    prefs.reducedMotion ||
    !prefs.enableAnimations ||
    (typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches)

  if (reduceMotion) {
    root.style.setProperty('--transition-fast', '0ms')
    root.style.setProperty('--transition-base', '0ms')
    root.style.setProperty('--transition-slow', '0ms')
  }

  // 毛玻璃：用户明确关闭则移除 backdrop-filter（与主题 card.style 解耦）
  root.classList.toggle('no-glassmorphism', !prefs.enableGlassmorphism)
}

export const useThemeStore = create<ThemeState & ThemeActions>()(
  persist(
    (set, get) => ({
      // 初始状态
      mode: 'dark',
      currentThemeId: 'dark',
      resolvedTheme: 'dark',
      themeConfig: null,
      availableThemes: [],
      isLoading: false,

      // 设置主题模式
      setMode: (mode) => {
        const resolvedTheme = resolveThemeMode(mode)
        const newThemeId = mode === 'system' ? resolvedTheme : mode
        set({ mode, resolvedTheme })
        // 如果模式改变导致主题改变，重新加载
        if (newThemeId !== get().currentThemeId) {
          get().loadTheme(newThemeId)
        } else {
          get().applyTheme()
        }
      },

      // 切换到指定主题
      setTheme: async (themeId) => {
        set({ currentThemeId: themeId, mode: themeId as ThemeMode })
        await get().loadTheme(themeId)
      },

      // 加载主题配置
      loadTheme: async (themeId) => {
        set({ isLoading: true })
        try {
          let config: ThemeConfig | null = null

          // 1. 先尝试从预设主题加载
          config = getPresetTheme(themeId)

          // 2. 如果预设主题不存在，尝试从用户主题加载
          if (!config) {
            const userTheme = ThemeStorageService.getUserTheme(themeId)
            if (userTheme) {
              // 加载基础主题
              const baseTheme = getPresetTheme(userTheme.basedOn)
              if (baseTheme) {
                // 合并用户自定义配置
                config = mergeTheme(baseTheme, userTheme.customizations)
                config.id = userTheme.id
                config.name = userTheme.name
              }
            }
          }

          if (config) {
            // 判断是否为浅色主题
            const resolved = isLightTheme(config) ? 'light' : 'dark'
            set({
              themeConfig: config,
              currentThemeId: themeId,
              resolvedTheme: resolved,
            })
            get().applyTheme()
          } else {
            console.error(`无法加载主题: ${themeId}`)
            // 回退到深色主题
            const fallback = getPresetTheme('dark')
            if (fallback) {
              set({
                themeConfig: fallback,
                currentThemeId: 'dark',
                resolvedTheme: 'dark',
              })
              get().applyTheme()
            }
          }
        } catch (error) {
          console.error('加载主题失败:', error)
          // 回退到内置主题
          const fallback = getPresetTheme('dark')
          if (fallback) {
            set({
              themeConfig: fallback,
              currentThemeId: 'dark',
              resolvedTheme: 'dark',
            })
            get().applyTheme()
          }
        } finally {
          set({ isLoading: false })
        }
      },

      // 加载用户自定义主题
      loadUserThemes: () => {
        // 用户主题会在 updateAvailableThemes 中合并到 availableThemes
        get().updateAvailableThemes()
      },

      // 更新可用主题列表
      updateAvailableThemes: () => {
        const userThemes = ThemeStorageService.getUserThemes()

        // 合并预设主题和用户主题
        const allThemes: ThemeInfo[] = [
          ...themeList,
          ...userThemes.map((theme) => ({
            id: theme.id,
            name: theme.name,
            description: `基于 ${theme.basedOn} 的自定义主题`,
            category: 'special' as const,
            preview: {
              primary: '#8b5cf6',
              background: '#0f172a',
              surface: '#1e293b',
              text: '#f8fafc',
              accent: '#06b6d4',
            },
          })),
        ]

        set({ availableThemes: allThemes })
      },

      // 重置主题
      resetTheme: () => {
        set({
          mode: 'dark',
          currentThemeId: 'dark',
          resolvedTheme: 'dark',
        })
        get().loadTheme('dark')
      },

      refreshThemes: () => {
        get().updateAvailableThemes()
      },

      // 应用主题到 DOM
      applyTheme: () => {
        const { themeConfig } = get()
        if (!themeConfig) return
        // 使用优化后的批量应用方法（含 effects → 全局过渡/动画变量）
        applyThemeToDOM(themeConfig)

        const root = document.documentElement
        const body = document.body
        const { backgrounds } = themeConfig

        // === 用户偏好覆盖（ThemePreferences）===
        // 主题 effects 是默认值，用户偏好是最终决定权：
        // reducedMotion / enableAnimations=false → 强制过渡归零（无障碍）
        // enableGlassmorphism=false → 关闭毛玻璃（覆盖主题 card.style:'glass'）
        applyMotionPreferences(root)

        // 背景图片
        if (backgrounds.image?.enabled && backgrounds.image?.url) {
          body.classList.add('has-bg-image')
          root.style.setProperty('--bg-image', `url(${backgrounds.image.url})`)
          root.style.setProperty('--bg-image-position', backgrounds.image.position)
          root.style.setProperty('--bg-image-size', backgrounds.image.size)
          root.style.setProperty('--bg-overlay', backgrounds.image.overlay)
          root.style.setProperty('--bg-overlay-opacity', String(backgrounds.image.overlayOpacity))
        } else {
          body.classList.remove('has-bg-image')
        }

        // 纹理
        if (backgrounds.texture) {
          const textureCSS = generateTextureCSS(backgrounds.texture)
          root.style.setProperty('--bg-texture', textureCSS)
          root.style.setProperty('--bg-texture-size', backgrounds.texture.size || '24px')
          root.style.setProperty('--bg-texture-opacity', String(backgrounds.texture.opacity || 0.1))
        }
      },
    }),
    {
      name: 'theme-storage',
      // 配额满时吞掉 QuotaExceededError，避免 setMode/setTheme 等 action 崩溃
      storage: createTolerantStorage(),
      partialize: (state) => ({
        mode: state.mode,
        currentThemeId: state.currentThemeId,
      }),
    },
  ),
)

/**
 * 初始化主题（在应用启动时调用）
 */
export async function initializeTheme() {
  const store = useThemeStore.getState()

  // 先拉取动态主题（后端无状态清单 → fetch JSON → 存 localStorage），
  // 必须在 updateAvailableThemes 之前完成，否则新主题不会被合并进列表。
  // 内部已做降级：后端不可达时静默返回，不影响内置 preset。
  await fetchDynamicThemes()

  // 更新可用主题列表（preset + localStorage 用户主题，含上一步加载的动态主题）
  store.updateAvailableThemes()

  // 解析当前主题
  const resolvedTheme = resolveThemeMode(store.mode)
  useThemeStore.setState({ resolvedTheme })

  // 加载主题配置
  await store.loadTheme(store.currentThemeId)

  // 监听系统主题变化
  if (typeof window !== 'undefined') {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    mediaQuery.addEventListener('change', () => {
      if (store.mode === 'system') {
        const newResolved = getSystemTheme()
        useThemeStore.setState({ resolvedTheme: newResolved })
        store.loadTheme(newResolved)
      }
    })
  }
}
