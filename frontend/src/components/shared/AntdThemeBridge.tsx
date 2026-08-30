/**
 * AntdThemeBridge — 把应用主题下发给 antd 组件体系
 *
 * 真实缺陷：@rjsf/antd 表单（设置页/Agent 配置/会话编辑/聊天 widget）与
 * antd Select/Switch 走 antd 自带 token；应用根部此前无 ConfigProvider，
 * antd 恒为浅色默认（Form.Item label rgba(0,0,0,0.88)、输入框白底），
 * 深色主题下压在应用深色面板上不可读（模态框说明文字撞色）。
 *
 * 机制：antd v6 组件按 --ant-* CSS 变量消费 token，ConfigProvider 在
 * React 树根部统一下发——resolvedTheme 切深浅算法（与 html 的 dark/light
 * class 同一判定源），主色/基准字色桥接主题令牌（映射逻辑见 ./antdTheme）。
 */

import { ConfigProvider } from 'antd'
import { type FC, useMemo, type ReactNode } from 'react'
import { useThemeStore } from '@/stores/themeStore'
import { toAntdTheme } from './antdTheme'

/** 应用根部 antd 主题提供者：主题切换时 antd 组件令牌同步刷新 */
export const AntdThemeProvider: FC<{ children: ReactNode }> = ({ children }) => {
  const themeConfig = useThemeStore((s) => s.themeConfig)
  const resolvedTheme = useThemeStore((s) => s.resolvedTheme)
  const antdThemeConfig = useMemo(
    () => toAntdTheme(themeConfig, resolvedTheme),
    [themeConfig, resolvedTheme],
  )
  return <ConfigProvider theme={antdThemeConfig}>{children}</ConfigProvider>
}
