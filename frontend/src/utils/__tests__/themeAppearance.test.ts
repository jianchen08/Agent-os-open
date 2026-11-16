/**
 * @feature: FP-T12 前端适配（UX-X8-F1/P-74/P-75） | @ci: frontend-test
 * 首帧主题外观专用键（theme-appearance）写入单元测试
 *
 * 契约：themeStore.applyTheme 每次成功应用主题时经 writeThemeAppearance 同步写
 * 分户专用键 'theme-appearance'（值恒为 'dark'|'light' 纯字符串），供
 * public/theme-init.js 首帧判定（模块头注释与台账 P-74/P-75 为真值锚）。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { writeThemeAppearance } from '@/utils/themeAppearance'
import { setCacheUserId } from '@/utils/userScopedStorage'

describe('themeAppearance 专用键写入', () => {
  beforeEach(() => {
    localStorage.clear()
    setCacheUserId(null)
  })

  it('匿名态写入 ag-anon 命名空间，值为纯字符串 dark/light（非 JSON 包装）', () => {
    writeThemeAppearance('dark')
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('dark')
    writeThemeAppearance('light')
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBe('light')
  })

  it('登录态写入本人命名空间，不同账户键互不可达（分户隔离）', () => {
    setCacheUserId('u-1')
    writeThemeAppearance('dark')
    expect(localStorage.getItem('ag-u:u-1:theme-appearance')).toBe('dark')
    expect(localStorage.getItem('ag-anon:theme-appearance')).toBeNull()
    setCacheUserId('u-2')
    writeThemeAppearance('light')
    // u-1 的既有键不被 u-2 的写入触碰（各用各键）
    expect(localStorage.getItem('ag-u:u-1:theme-appearance')).toBe('dark')
    expect(localStorage.getItem('ag-u:u-2:theme-appearance')).toBe('light')
  })
})
