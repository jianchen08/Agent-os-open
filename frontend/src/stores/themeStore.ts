/**
 * 主题状态管理
 *
 * 从前端预设主题和用户自定义主题加载配置
 * 支持 light/dark/system 模式
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { themeList } from '@/config/themes'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import {
  getPresetTheme,
  applyTheme as applyThemeToDOM,
  applyPluginThemeVars,
  clearPluginThemeVars,
  derivePluginThemePreview,
  fetchDynamicThemes,
} from '@/services/themeService'
import { ThemeStorageService, mergeTheme } from '@/services/themeStorage'
import { applyPluginSkin, clearPluginSkin, isSkinTheme } from '@/services/skinRuntime'
import { loggers } from '@/utils/logger'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import type { PluginTheme, ThemeConfig, ThemeInfo, ThemeMode } from '@/types/theme'

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
  /** 当前生效的插件主题（contributes.themes；null 表示无插件主题覆盖） */
  activePluginTheme: PluginTheme | null
  /** 会话恢复时 registry 未就绪而挂起的主题 id（schema 注册后重放） */
  pendingThemeId: string | null
  /** 可用主题列表（预设 + 用户自定义 + 插件贡献） */
  availableThemes: ThemeInfo[]
  /** 是否正在加载 */
  isLoading: boolean
  /** AI 消息气泡形态（'flat'=平铺跟 DeepSeek/DSH 原生；'bubble'=气泡；主题声明可开关） */
  bubbleAiMode: 'flat' | 'bubble'
  /** 背景图激活信号（body.has-bg-image：背景图主题或皮肤激活）。平铺 AI 消息
      在背景图上需气泡面（用户裁决：文字不许裸贴背景图，但只框气泡区域）——
      React 内联样式无法被 CSS 覆盖，故由本状态驱动替换透明底 */
  bgImageActive: boolean
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
  /** schema 注册完成后重放挂起的插件主题（会话恢复时序） */
  retryPendingTheme: () => Promise<void>
  /** 应用主题到 DOM */
  applyTheme: () => void
  /** 重置为默认主题 */
  resetTheme: () => void
  /** 更新可用主题列表 */
  updateAvailableThemes: () => void
  /** 刷新主题列表 */
  refreshThemes: () => void
  /**
   * 同步插件主题（schema 重载后调用）
   *
   * 1. 插件主题合入 availableThemes；
   * 2. 当前正在用的插件主题被移除（插件禁用/卸载）→ 回退其 base 主题；
   * 3. 当前主题是插件主题但尚未应用（启动时序：主题初始化先于 schema 加载）→ 补应用。
   */
  syncPluginThemes: () => void
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
    case 'checker':
      // 25% 象限棋盘：配合 background-size 形成像素风棋盘格
      return `repeating-conic-gradient(${color} 0% 25%, transparent 0% 50%)`
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
      activePluginTheme: null,
      pendingThemeId: null,
      availableThemes: [],
      isLoading: false,
      bubbleAiMode: 'bubble',
      bgImageActive: false,

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
            if (userTheme && themeId.startsWith('dsh-skin-')) {
              // dsh-skin-* 前缀的用户主题为无效残留数据（皮肤走
              // contributes.themes 插件主题通道，不落用户主题分支）——
              // 用户主题分支优先于插件主题，残留会截胡皮肤选择，清除后
              // 落到下方 pluginTheme 分支。
              ThemeStorageService.deleteUserTheme(themeId)
            } else if (userTheme) {
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

          // 3. 插件主题（contributes.themes）：以 base 主题配置为基础，
          //    插件声明的 CSS 变量在 applyTheme 之后按 setProperty 覆盖（后写者胜）。
          //    纯数据无 JS 执行——主题插件是"大众级定制"的正路（任务文档第 1 层）。
          let pluginTheme: PluginTheme | null = null
          if (!config) {
            pluginTheme = contributionRegistry.getPluginTheme(themeId) ?? null
            if (pluginTheme) {
              const baseTheme = getPresetTheme(pluginTheme.base)
              if (baseTheme) {
                config = baseTheme
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
              activePluginTheme: pluginTheme,
            })
            get().applyTheme()
          } else if (!contributionRegistry.isInitialized()) {
            // 插件主题（如 DSH 皮肤）在会话恢复时序里早于 growthLoop 的 schema
            // 注册——registry 未就绪时查不到≠主题不存在：挂起等待（不回退
            // dark、不覆盖持久化选择；retryPendingTheme 在注册完成后重放）。
            // 回退+persist 会把用户选择永久改写成 dark（点皮肤卡生效→刷新→
            // 被打回 dark 且 themeId 被覆盖）。
            set({ pendingThemeId: themeId, isLoading: false })
            return
          } else {
            console.error(`无法加载主题: ${themeId}`)
            // 回退到深色主题
            const fallback = getPresetTheme('dark')
            if (fallback) {
              set({
                themeConfig: fallback,
                currentThemeId: 'dark',
                resolvedTheme: 'dark',
                activePluginTheme: null,
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
              activePluginTheme: null,
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

      // schema 注册完成后重放挂起的插件主题（grewLoop 调用）
      retryPendingTheme: async () => {
        const pending = get().pendingThemeId
        if (!pending) return
        if (!contributionRegistry.isInitialized()) return
        const hit =
          getPresetTheme(pending) ||
          ThemeStorageService.getUserTheme(pending) ||
          contributionRegistry.getPluginTheme(pending)
        if (hit) {
          set({ pendingThemeId: null })
          await get().loadTheme(pending)
        }
      },

      // 更新可用主题列表
      updateAvailableThemes: () => {
        const userThemes = ThemeStorageService.getUserThemes()

        // 插件贡献的主题（contributes.themes）：来源插件标注 pluginId，
        // 预览色由声明的 --ds-* 变量派生（缺省回退 base 主题预览）。
        const pluginThemes: ThemeInfo[] = contributionRegistry.getPluginThemes().map((t) => ({
          id: t.id,
          name: t.name,
          description: t.description ?? `来自插件 ${t.pluginId} 的主题`,
          category: t.base,
          pluginId: t.pluginId,
          preview: derivePluginThemePreview(t),
        }))

        // 合并预设主题 + 插件主题 + 用户主题
        const allThemes: ThemeInfo[] = [
          ...themeList,
          ...pluginThemes,
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

      // 同步插件主题（schema 重载后由 GrowthLoop 调用）
      syncPluginThemes: () => {
        get().updateAvailableThemes()

        const { currentThemeId, activePluginTheme, themeConfig } = get()
        const pluginTheme = contributionRegistry.getPluginTheme(currentThemeId)

        if (pluginTheme) {
          // 当前主题是插件主题：若尚未应用（启动时序：主题初始化先于 schema 加载，
          // 或插件被重新启用），补一次 loadTheme 让变量生效。
          const applied = activePluginTheme?.id === pluginTheme.id && themeConfig?.id === pluginTheme.id
          if (!applied) {
            void get().loadTheme(currentThemeId)
          }
        } else if (activePluginTheme) {
          // 当前正在用的插件主题已从注册表消失（插件被禁用/卸载）→ 回退其 base 主题，
          // 无残留样式（applyTheme 全量重写变量，插件变量不再被写入）。
          loggers.websocket.info(
            `[themeStore] 插件主题 ${activePluginTheme.id} 已移除（插件 ${activePluginTheme.pluginId} 禁用/卸载），回退 ${activePluginTheme.base}`,
          )
          void get().loadTheme(activePluginTheme.base)
        }
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
        const { themeConfig, activePluginTheme } = get()
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

        // 背景图片（通用信号 body.has-bg-image：皮肤激活时由 skinRuntime
        // 挂同标记——hooks 画背景的皮肤与内置背景主题走同一"背景图上
        // 需卡面"规则；互斥：皮肤激活期主题管线的 image 分支不覆盖标记）
        const skinActive = get().activePluginTheme?.skin != null
        if (backgrounds.image?.enabled && backgrounds.image?.url && !skinActive) {
          body.classList.add('has-bg-image')
          root.style.setProperty('--bg-image', `url(${backgrounds.image.url})`)
          root.style.setProperty('--bg-image-position', backgrounds.image.position)
          root.style.setProperty('--bg-image-size', backgrounds.image.size)
          root.style.setProperty('--bg-image-attachment', backgrounds.image.attachment)
          root.style.setProperty('--bg-overlay', backgrounds.image.overlay)
          root.style.setProperty('--bg-overlay-opacity', String(backgrounds.image.overlayOpacity))
        } else {
          body.classList.remove('has-bg-image')
          root.style.removeProperty('--bg-image')
        }

        // 纹理（主背景）
        if (backgrounds.texture) {
          const textureCSS = generateTextureCSS(backgrounds.texture)
          root.style.setProperty('--bg-texture', textureCSS)
          root.style.setProperty('--bg-texture-size', backgrounds.texture.size || '24px')
          root.style.setProperty('--bg-texture-opacity', String(backgrounds.texture.opacity || 0.1))
        } else {
          root.style.setProperty('--bg-texture', 'none')
        }

        // 区域纹理（侧边栏 / 聊天区），叠加在各自区域背景色之上
        const areaTextures: Array<[key: string, tex: ThemeConfig['backgrounds']['texture']]> = [
          ['sidebar', backgrounds.sidebar?.texture],
          ['chat', backgrounds.chat?.texture],
        ]
        areaTextures.forEach(([key, tex]) => {
          if (tex) {
            root.style.setProperty(`--${key}-texture`, generateTextureCSS(tex))
            root.style.setProperty(`--${key}-texture-size`, tex.size || '24px')
          } else {
            root.style.setProperty(`--${key}-texture`, 'none')
          }
        })

        // === 插件主题覆盖（contributes.themes）===
        // 在 base 主题（含其背景）全部应用之后执行：插件声明的变量 setProperty 后写者胜，
        // 背景按 enabled 开关覆盖。纯数据无 JS 执行（主题插件是"大众级定制"的正路）。
        // 每次应用先清上一插件主题变量（皮肤切回内置主题时 --region-* 等残留
        // 会让区域背景/气泡形态停留在旧皮肤）。
        clearPluginThemeVars()
        if (activePluginTheme) {
          applyPluginThemeVars(activePluginTheme)
        }

        // AI 消息气泡形态（主题声明开关：插件主题经 --bubble-ai-mode 变量，
        // 内置主题经 colors.bubbles.ai_mode；默认 bubble 保既有行为）
        const bubbleAiMode: 'flat' | 'bubble' =
          activePluginTheme?.variables?.['--bubble-ai-mode'] === 'flat' ||
          (themeConfig.colors?.bubble?.ai_mode === 'flat')
            ? 'flat'
            : 'bubble'
        set({
          bubbleAiMode,
          // 背景图信号（与 body.has-bg-image 同源；皮肤激活时 applyPluginSkin
          // 异步挂类，此处直接按声明判定不依赖 DOM 时序）
          bgImageActive: skinActive || (backgrounds.image?.enabled && !!backgrounds.image?.url),
        })

        // === 皮肤运行时按择注入路由（声明驱动）===
        // 任何插件主题声明 skin 字段即获得全部皮肤能力：平台 scope 打标 +
        // 皮肤 CSS 按择注入 + hooks 动态层（六层装饰/背景/装饰条槽位）；
        // 其余主题 → 摘除。与主题变量同一选择源驱动。
        if (isSkinTheme(activePluginTheme)) {
          void applyPluginSkin(activePluginTheme)
        } else {
          clearPluginSkin()
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
 * 等待 zustand persist 完成从 localStorage 恢复
 * 避免 initializeTheme 在 rehydrate 前用默认 dark 覆盖用户选择的 light
 */
async function waitForThemeHydration(): Promise<void> {
  const persistApi = (useThemeStore as unknown as {
    persist?: {
      hasHydrated?: () => boolean
      onFinishHydration?: (fn: () => void) => () => void
    }
  }).persist

  if (!persistApi?.hasHydrated) return
  if (persistApi.hasHydrated()) return

  await new Promise<void>((resolve) => {
    const unsub = persistApi.onFinishHydration?.(() => {
      unsub?.()
      resolve()
    })
    // 兜底：极端情况下 rehydrate 未触发也不阻塞启动
    window.setTimeout(() => {
      unsub?.()
      resolve()
    }, 500)
  })
}

/**
 * 初始化主题（在应用启动时调用）
 */
export async function initializeTheme() {
  // 必须先完成 persist rehydrate，再读 currentThemeId
  await waitForThemeHydration()

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
      if (useThemeStore.getState().mode === 'system') {
        const newResolved = getSystemTheme()
        useThemeStore.setState({ resolvedTheme: newResolved })
        useThemeStore.getState().loadTheme(newResolved)
      }
    })
  }
}
