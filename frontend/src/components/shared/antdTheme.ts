/**
 * 应用主题配置 → antd 主题映射（纯函数，供 AntdThemeBridge 消费）
 *
 * 独立成模块以保持 AntdThemeBridge.tsx 纯组件导出（react-refresh 约束）。
 */

import { theme as antdTheme } from 'antd'
import { getPresetTheme } from '@/services/themeService'
import type { ThemeConfig } from '@/types/theme'
import type { ThemeConfig as AntdThemeConfig } from 'antd'

/**
 * 应用主题配置 → antd 主题
 *
 * @param config - 当前生效的主题配置（启动窗口期可能为 null，回退 dark 预设，
 *   与 index.html 内联脚本预挂的 dark class 同源）
 * @param resolvedTheme - themeStore 的深浅判定（special 族如 high-contrast 归 dark）
 */
export function toAntdTheme(
  config: ThemeConfig | null,
  resolvedTheme: 'light' | 'dark',
): AntdThemeConfig {
  const cfg = config ?? getPresetTheme('dark')
  if (!cfg) return {}
  return {
    algorithm:
      resolvedTheme === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: cfg.colors.primary,
      colorTextBase: cfg.colors.text.primary,
    },
  }
}
