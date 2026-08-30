/**
 * AntdThemeBridge — antd 主题桥测试
 *
 * 真实缺陷：@rjsf/antd 表单（设置页/Agent 配置/会话编辑/聊天 widget）的
 * Form.Item label、输入框走 antd 自带 token；全应用此前无 ConfigProvider，
 * antd 恒为浅色默认（label rgba(0,0,0,0.88)、输入框白底），深色主题下压在
 * 应用深色面板上不可读。契约：antd 主题由应用主题统一下发——resolvedTheme
 * 切深浅算法，主色/基准字色桥接主题令牌。
 */

import { render } from '@testing-library/react'
import { theme as antdTheme } from 'antd'
import { describe, it, expect, beforeEach } from 'vitest'
import { presetThemes } from '@/config/themes'
import { useThemeStore } from '@/stores/themeStore'
import { toAntdTheme } from '../antdTheme'
import { AntdThemeProvider } from '../AntdThemeBridge'

describe('toAntdTheme 主题映射', () => {
  it('深色主题 → darkAlgorithm，字色/主色桥接主题令牌', () => {
    const mapped = toAntdTheme(presetThemes['dark'], 'dark')
    expect(mapped.algorithm).toBe(antdTheme.darkAlgorithm)
    expect(mapped.token?.colorTextBase).toBe(presetThemes['dark'].colors.text.primary)
    expect(mapped.token?.colorPrimary).toBe(presetThemes['dark'].colors.primary)
  })

  it('浅色主题 → defaultAlgorithm，桥接同样生效', () => {
    const mapped = toAntdTheme(presetThemes['light'], 'light')
    expect(mapped.algorithm).toBe(antdTheme.defaultAlgorithm)
    expect(mapped.token?.colorTextBase).toBe(presetThemes['light'].colors.text.primary)
    expect(mapped.token?.colorPrimary).toBe(presetThemes['light'].colors.primary)
  })

  it('special 族按 resolvedTheme 判算法：high-contrast（深底）→ darkAlgorithm', () => {
    const mapped = toAntdTheme(presetThemes['high-contrast'], 'dark')
    expect(mapped.algorithm).toBe(antdTheme.darkAlgorithm)
  })

  it('themeConfig 未就绪（启动窗口期）→ 深色兜底（与 index.html 预挂 dark class 同源）', () => {
    const mapped = toAntdTheme(null, 'dark')
    expect(mapped.algorithm).toBe(antdTheme.darkAlgorithm)
  })
})

/** 读取 antd 下发 token 的探针组件 */
function TokenProbe({ read }: { read: (token: { colorTextBase?: string; colorBgContainer?: string }) => void }) {
  const { token } = antdTheme.useToken()
  read(token)
  return null
}

describe('AntdThemeProvider 令牌下发', () => {
  beforeEach(() => {
    useThemeStore.setState({ themeConfig: null, resolvedTheme: 'dark' })
  })

  it('性质：store 里的主题配置驱动 antd token，深色下容器底为深色（输入框白底岛回归锁）', () => {
    useThemeStore.setState({ themeConfig: presetThemes['dark'], resolvedTheme: 'dark' })
    let captured: { colorTextBase?: string; colorBgContainer?: string } = {}
    render(
      <AntdThemeProvider>
        <TokenProbe read={(t) => (captured = t)} />
      </AntdThemeProvider>,
    )
    expect(captured.colorTextBase).toBe(presetThemes['dark'].colors.text.primary)
    // 深色算法下容器底应为深色（输入框/下拉不再以白底压深色面板）
    const bg = captured.colorBgContainer ?? ''
    expect(parseInt(bg.replace(/\s/g, '').slice(1, 3), 16)).toBeLessThan(64)
  })

  it('切换主题后 token 随 store 更新（深 → 浅）', () => {
    useThemeStore.setState({ themeConfig: presetThemes['dark'], resolvedTheme: 'dark' })
    let captured: { colorTextBase?: string } = {}
    const { rerender } = render(
      <AntdThemeProvider>
        <TokenProbe read={(t) => (captured = t)} />
      </AntdThemeProvider>,
    )
    expect(captured.colorTextBase).toBe(presetThemes['dark'].colors.text.primary)

    useThemeStore.setState({ themeConfig: presetThemes['light'], resolvedTheme: 'light' })
    rerender(
      <AntdThemeProvider>
        <TokenProbe read={(t) => (captured = t)} />
      </AntdThemeProvider>,
    )
    expect(captured.colorTextBase).toBe(presetThemes['light'].colors.text.primary)
  })
})
