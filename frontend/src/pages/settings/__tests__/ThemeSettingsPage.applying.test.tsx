// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * 主题应用 pending 反馈测试（#10）：
 * - store：setTheme await 窗口 applyingThemeId 置位、落定清除（失败/成功皆清）
 * - 页面：应用卡转圈 +「应用中…」，应用期间全卡禁用防连点/并发应用
 */
import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useThemeStore } from '@/stores/themeStore'
import { ThemeSettingsPage } from '../ThemeSettingsPage'

beforeEach(() => {
  vi.clearAllMocks()
  useThemeStore.setState({ applyingThemeId: null, currentThemeId: 'light' })
})

afterEach(() => {
  cleanup()
})

describe('themeStore applyingThemeId（setTheme await 窗口）', () => {
  it('loadTheme pending 期间置位，落定清除', async () => {
    let resolveLoad: () => void = () => {}
    useThemeStore.setState({
      loadTheme: () =>
        new Promise<void>((resolve) => {
          resolveLoad = resolve
        }),
    })

    const promise = useThemeStore.getState().setTheme('dark')
    expect(useThemeStore.getState().applyingThemeId).toBe('dark')

    resolveLoad()
    await promise
    expect(useThemeStore.getState().applyingThemeId).toBeNull()
  })

  it('loadTheme 失败：applyingThemeId 同样清除（不残留禁用）', async () => {
    useThemeStore.setState({
      loadTheme: () => Promise.reject(new Error('theme load boom')),
    })
    await expect(useThemeStore.getState().setTheme('dark')).rejects.toThrow('theme load boom')
    expect(useThemeStore.getState().applyingThemeId).toBeNull()
  })
})

describe('ThemeSettingsPage 主题卡 pending 反馈', () => {
  it('applyingThemeId 命中卡：转圈 +「应用中…」；应用期间全部卡禁用', () => {
    useThemeStore.setState({ applyingThemeId: 'dark', currentThemeId: 'light' })
    render(<ThemeSettingsPage embedded />)

    const applyingCard = screen.getByTestId('theme-card-dark')
    expect(applyingCard).toBeDisabled()
    expect(screen.getByText('应用中…')).toBeInTheDocument()
    expect(applyingCard.querySelector('.animate-spin')).not.toBeNull()

    // 并发防护：其他卡同被禁用
    expect(screen.getByTestId('theme-card-light')).toBeDisabled()
  })

  it('空闲态：卡片可用、无应用中文案', () => {
    useThemeStore.setState({ applyingThemeId: null, currentThemeId: 'light' })
    render(<ThemeSettingsPage embedded />)

    expect(screen.getByTestId('theme-card-dark')).toBeEnabled()
    expect(screen.queryByText('应用中…')).not.toBeInTheDocument()
  })

  it('点击卡片 → setTheme 触发 + applyingThemeId 置位（乐观高亮随 currentThemeId 走）', async () => {
    useThemeStore.setState({
      loadTheme: () => new Promise<void>(() => {}), // 挂起模拟 await 窗口
      refreshThemes: () => {},
    })
    render(<ThemeSettingsPage embedded />)

    fireEvent.click(screen.getByTestId('theme-card-dark'))
    await waitFor(() => expect(useThemeStore.getState().applyingThemeId).toBe('dark'))
    // 乐观高亮：currentThemeId 已切 → 目标卡即显当前使用
    expect(useThemeStore.getState().currentThemeId).toBe('dark')
  })
})
