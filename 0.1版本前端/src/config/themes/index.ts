/**
 * 主题配置导出
 *
 * 集中管理所有预设主题配置
 */

import { darkTheme } from './presets/dark'
import { deepSpaceTheme } from './presets/deep-space'
import { highContrastTheme } from './presets/high-contrast'
import { lightTheme } from './presets/light'
import { oceanBreezeTheme } from './presets/ocean-breeze'
import type { ThemeInfo, ThemeConfig } from '@/types/theme'

// 重新导出主题对象，方便直接导入
export { darkTheme } from './presets/dark'
export { lightTheme } from './presets/light'
export { deepSpaceTheme } from './presets/deep-space'
export { oceanBreezeTheme } from './presets/ocean-breeze'
export { highContrastTheme } from './presets/high-contrast'

/**
 * 预设主题映射表
 *
 * 用于通过主题 ID 快速获取主题配置
 */
export const presetThemes: Record<string, ThemeConfig> = {
  dark: darkTheme,
  light: lightTheme,
  'deep-space': deepSpaceTheme,
  'ocean-breeze': oceanBreezeTheme,
  'high-contrast': highContrastTheme,
}

/**
 * 预设主题列表
 *
 * 用于 UI 展示主题选择器
 */
export const themeList: ThemeInfo[] = [
  // 基础主题
  {
    id: 'dark',
    name: '深色主题',
    description: '默认深色主题，适合夜间使用',
    category: 'dark',
    preview: {
      primary: '#3b82f6',
      background: '#0f172a',
      surface: '#1e293b',
      text: '#f8fafc',
      accent: '#8b5cf6',
    },
  },
  {
    id: 'light',
    name: '浅色主题',
    description: '默认浅色主题，适合日间使用',
    category: 'light',
    preview: {
      primary: '#2563eb',
      background: '#ffffff',
      surface: '#f8fafc',
      text: '#0f172a',
      accent: '#7c3aed',
    },
  },

  // 特殊主题
  {
    id: 'deep-space',
    name: '深空指挥台',
    description: '专业级深色主题，模拟太空指挥中心的科技感界面',
    category: 'dark',
    preview: {
      primary: '#00f0ff',
      background: '#020617',
      surface: '#0f172a',
      text: '#f8fafc',
      accent: '#7c3aed',
    },
  },
  {
    id: 'ocean-breeze',
    name: '海洋微风',
    description: '清新自然的蓝绿色调，带来海洋般的宁静感受',
    category: 'light',
    preview: {
      primary: '#0891b2',
      background: '#f0fdff',
      surface: '#e6fffa',
      text: '#0f172a',
      accent: '#06b6d4',
    },
  },

  // 无障碍主题
  {
    id: 'high-contrast',
    name: '高对比度',
    description: '专为视觉障碍用户设计的高对比度主题，符合WCAG 2.1 AAA标准',
    category: 'special',
    preview: {
      primary: '#ffffff',
      background: '#000000',
      surface: '#1a1a1a',
      text: '#ffffff',
      accent: '#ffff00',
    },
  },
]

/**
 * 获取主题配置
 *
 * @param themeId - 主题 ID
 * @returns 主题配置，如果不存在则返回 null
 */
export function getThemeById(themeId: string): ThemeConfig | null {
  return presetThemes[themeId] || null
}

/**
 * 获取所有预设主题
 *
 * @returns 预设主题数组
 */
export function getAllPresetThemes(): ThemeConfig[] {
  return Object.values(presetThemes)
}

/**
 * 检查主题是否存在
 *
 * @param themeId - 主题 ID
 * @returns 是否存在
 */
export function hasTheme(themeId: string): boolean {
  return themeId in presetThemes
}
