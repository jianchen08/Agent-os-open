/**
 * 主题存储服务
 *
 * 管理用户自定义主题的 localStorage 存储
 */

import { storage } from '@/utils/storage'
import type { ThemeConfig } from '@/types/theme'

/**
 * localStorage 键名常量
 */
const STORAGE_KEYS = {
  ACTIVE_THEME: 'theme_active',
  USER_THEMES: 'theme_user_custom',
  PREFERENCES: 'theme_preferences',
} as const

/**
 * 用户主题配置
 *
 * 用户基于预设主题创建的自定义主题
 */
export interface UserThemeConfig {
  /** 主题 ID */
  id: string
  /** 主题名称 */
  name: string
  /** 基于哪个预设主题 */
  basedOn: string
  /** 用户自定义的配置（只包含修改过的部分） */
  customizations: Partial<ThemeConfig>
  /** 创建时间 */
  createdAt: string
  /** 更新时间 */
  updatedAt: string
}

/**
 * 主题偏好设置
 */
export interface ThemePreferences {
  /** 是否跟随系统主题 */
  followSystem: boolean
  /** 是否启用动画 */
  enableAnimations: boolean
  /** 是否启用毛玻璃效果 */
  enableGlassmorphism: boolean
  /** 是否减少动画 */
  reducedMotion: boolean
}

/** 默认主题偏好设置 */
const DEFAULT_PREFERENCES: ThemePreferences = {
  followSystem: false,
  enableAnimations: true,
  enableGlassmorphism: true,
  reducedMotion: false,
}

/**
 * 主题存储服务类
 */
export class ThemeStorageService {
  /**
   * 获取当前激活的主题 ID
   */
  static getActiveTheme(): string {
    return storage.getItem<string>(STORAGE_KEYS.ACTIVE_THEME) || 'dark'
  }

  /**
   * 设置当前激活的主题 ID
   */
  static setActiveTheme(themeId: string): void {
    storage.setItem(STORAGE_KEYS.ACTIVE_THEME, themeId)
  }

  /**
   * 获取所有用户主题
   */
  static getUserThemes(): UserThemeConfig[] {
    return storage.getItem<UserThemeConfig[]>(STORAGE_KEYS.USER_THEMES) || []
  }

  /**
   * 获取指定用户主题
   */
  static getUserTheme(id: string): UserThemeConfig | null {
    const themes = this.getUserThemes()
    return themes.find((theme) => theme.id === id) || null
  }

  /**
   * 保存用户主题
   */
  static saveUserTheme(theme: UserThemeConfig): void {
    const themes = this.getUserThemes()
    const index = themes.findIndex((t) => t.id === theme.id)

    // 更新时间
    theme.updatedAt = new Date().toISOString()

    if (index >= 0) {
      // 更新现有主题
      themes[index] = theme
    } else {
      // 添加新主题
      if (!theme.createdAt) {
        theme.createdAt = new Date().toISOString()
      }
      themes.push(theme)
    }

    storage.setItem(STORAGE_KEYS.USER_THEMES, themes)
  }

  /**
   * 删除用户主题
   */
  static deleteUserTheme(themeId: string): boolean {
    const themes = this.getUserThemes()
    const filtered = themes.filter((t) => t.id !== themeId)

    if (filtered.length === themes.length) {
      return false // 没有找到要删除的主题
    }

    storage.setItem(STORAGE_KEYS.USER_THEMES, filtered)

    // 如果删除的是当前主题，重置为默认主题
    if (this.getActiveTheme() === themeId) {
      this.setActiveTheme('dark')
    }

    return true
  }

  /**
   * 导出主题配置为 JSON 字符串
   */
  static exportTheme(themeId: string): string {
    const theme = this.getUserTheme(themeId)

    if (!theme) {
      throw new Error(`主题 ${themeId} 不存在`)
    }

    return JSON.stringify(theme, null, 2)
  }

  /**
   * 导入主题配置
   */
  static importTheme(configJson: string): UserThemeConfig {
    const config = JSON.parse(configJson) as UserThemeConfig

    // 验证必需字段
    if (!config.id || !config.name || !config.basedOn) {
      throw new Error('主题配置无效：缺少必需字段')
    }

    // 检查是否已存在同名主题
    const existing = this.getUserTheme(config.id)
    if (existing) {
      // 更新现有主题
      config.updatedAt = new Date().toISOString()
      config.createdAt = existing.createdAt
    }

    this.saveUserTheme(config)
    return config
  }

  /**
   * 获取主题偏好设置
   */
  static getPreferences(): ThemePreferences {
    return storage.getItem<ThemePreferences>(STORAGE_KEYS.PREFERENCES) || DEFAULT_PREFERENCES
  }

  /**
   * 保存主题偏好设置
   */
  static savePreferences(preferences: Partial<ThemePreferences>): void {
    const current = this.getPreferences()
    const updated = { ...current, ...preferences }
    storage.setItem(STORAGE_KEYS.PREFERENCES, updated)
  }

  /**
   * 清除所有主题数据
   *
   * @warning 此操作不可逆
   */
  static clearAll(): void {
    storage.removeItem(STORAGE_KEYS.ACTIVE_THEME)
    storage.removeItem(STORAGE_KEYS.USER_THEMES)
    storage.removeItem(STORAGE_KEYS.PREFERENCES)
  }

  /**
   * 获取存储使用情况
   */
  static getStorageInfo(): {
    used: number
    total: number
    percentage: number
  } {
    let used = 0

    for (const key of Object.values(STORAGE_KEYS)) {
      const value = localStorage.getItem(key)
      if (value) {
        used += value.length
      }
    }

    // localStorage 通常限制为 5-10MB
    const total = 5 * 1024 * 1024 // 5MB
    const percentage = (used / total) * 100

    return { used, total, percentage }
  }
}

/**
 * 主题合并工具函数
 *
 * 将用户的自定义配置与基础预设主题合并
 */
export function mergeTheme(
  base: ThemeConfig,
  custom: UserThemeConfig['customizations'],
): ThemeConfig {
  // @ts-ignore - 复杂的类型合并问题，暂时忽略
  return {
    ...base,
    id: (custom.id as string) || base.id,
    name: (custom.name as string) || base.name,
    colors: {
      ...base.colors,
      ...custom.colors,
      background: {
        ...base.colors.background,
        ...custom.colors?.background,
      },
      text: {
        ...base.colors.text,
        ...custom.colors?.text,
      },
      border: {
        ...base.colors.border,
        ...custom.colors?.border,
      },
      status: {
        ...base.colors.status,
        ...custom.colors?.status,
      },
      bubble: {
        ...base.colors.bubble,
        ...custom.colors?.bubble,
      },
    },
    components: {
      ...base.components,
      ...custom.components,
      borderRadius: {
        ...base.components.borderRadius,
        ...custom.components?.borderRadius,
      },
      shadows: {
        ...base.components.shadows,
        ...custom.components?.shadows,
      },
      button: {
        ...base.components.button,
        variants: {
          ...base.components.button.variants,
          ...custom.components?.button?.variants,
        },
      },
    },
    effects: {
      ...base.effects,
      ...custom.effects,
    },
    backgrounds: {
      ...base.backgrounds,
      ...custom.backgrounds,
    },
  }
}
