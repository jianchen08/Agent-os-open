/**
 * @feature: FP-T12 前端适配（UX-X8-F1/P-74/P-75） | @ci: frontend-test
 * public/theme-init.js 首帧 dark 判定链行为测试
 *
 * theme-init.js 是 index.html 同步外链的 public/ 静态文件（生产 CSP 禁内联
 * script，先于打包产物执行，不可 import src 模块）——本测试读其真实文本，
 * 以 eval 桩 localStorage/document 冒烟，断言首帧判定链：
 * 1. 分户 theme-appearance 专用键（权威，themeStore.applyTheme 同步写，
 *    天然覆盖插件/用户深色主题）；
 * 2. 分户 theme-storage（zustand persist 实际落盘键）currentThemeId ×
 *    预设深色集（迁移窗口兜底）；
 * 3. 历史裸键 theme-storage（分户改造前数据迁移兼容）。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'

// vitest 以 frontend/ 为运行根（同 cspSecurityHeaders.contract.test.ts 先例）
const scriptSrc = readFileSync(resolve(process.cwd(), 'public/theme-init.js'), 'utf-8')

/** 以桩 localStorage/document 执行脚本真实文本，返回 <html> 上被加的 class 列表 */
function runThemeInit(storage: Record<string, string>): string[] {
  const added: string[] = []
  const fakeLocalStorage = {
    getItem: (key: string) => (key in storage ? storage[key] : null),
  }
  const fakeDocument = {
    documentElement: {
      classList: {
        add: (cls: string) => {
          added.push(cls)
        },
      },
    },
  }
  new Function('localStorage', 'document', scriptSrc)(fakeLocalStorage, fakeDocument)
  return added
}

/** zustand persist 落盘格式（partialize 只存 mode + currentThemeId） */
const persistPayload = (id: string): string =>
  JSON.stringify({ state: { mode: id, currentThemeId: id } })

describe('theme-init.js 首帧 dark 判定链', () => {
  it('匿名分户专用键 dark → 加 dark class', () => {
    expect(runThemeInit({ 'ag-anon:theme-appearance': 'dark' })).toEqual(['dark'])
  })

  it('登录态读本人命名空间专用键，他人/匿名键不可达（分户隔离）', () => {
    const ownKey = {
      auth_user: JSON.stringify({ id: 'u-7' }),
      'ag-u:u-7:theme-appearance': 'dark',
      'ag-anon:theme-appearance': 'dark',
    }
    expect(runThemeInit(ownKey)).toEqual(['dark'])
    const otherAccount = {
      auth_user: JSON.stringify({ id: 'u-7' }),
      'ag-u:u-other:theme-appearance': 'dark',
    }
    expect(runThemeInit(otherAccount)).toEqual([])
  })

  it('AUTH_USER 畸形按匿名命名空间解析（与 userScopedStorage 同向）', () => {
    expect(runThemeInit({ auth_user: 'not-json{', 'ag-anon:theme-appearance': 'dark' })).toEqual([
      'dark',
    ])
  })

  it('专用键优先级最高：light 压过 theme-storage 的深色 id，不置 dark', () => {
    expect(
      runThemeInit({
        'ag-anon:theme-appearance': 'light',
        'ag-anon:theme-storage': persistPayload('deep-space'),
      }),
    ).toEqual([])
  })

  it('专用键 dark 独立生效，不受 theme-storage 浅色 id 影响', () => {
    expect(
      runThemeInit({
        'ag-anon:theme-appearance': 'dark',
        'ag-anon:theme-storage': persistPayload('light'),
      }),
    ).toEqual(['dark'])
  })

  it('迁移窗口兜底：分户 theme-storage 深色预设 id → 加 dark class（浅色 id 不加）', () => {
    expect(runThemeInit({ 'ag-anon:theme-storage': persistPayload('high-contrast') })).toEqual([
      'dark',
    ])
    expect(runThemeInit({ 'ag-anon:theme-storage': persistPayload('moe-soft') })).toEqual([])
  })

  it('旧裸键迁移兼容：分户改造前 theme-storage 深色 id → 加 dark class', () => {
    expect(runThemeInit({ 'theme-storage': persistPayload('deep-space') })).toEqual(['dark'])
    expect(runThemeInit({ 'theme-storage': persistPayload('ocean-breeze') })).toEqual([])
  })

  it('light 场景不误置 dark：空存储/专用键 light/专用键异值/裸键畸形均不加 class', () => {
    expect(runThemeInit({})).toEqual([])
    expect(runThemeInit({ 'ag-anon:theme-appearance': 'light' })).toEqual([])
    expect(runThemeInit({ 'ag-anon:theme-appearance': 'blue' })).toEqual([])
    expect(runThemeInit({ 'theme-storage': '{corrupt' })).toEqual([])
  })
})
