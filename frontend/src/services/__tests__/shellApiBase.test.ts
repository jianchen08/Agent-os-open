/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * services/shellApiBase 纯函数分支：壳注入对象的存在/缺失/形状三态 +
 * 连接基址优先级（用户设置 > 壳注入 > 同源）。
 * 移动壳本地直用模式的 API 基址单点（消费方 = constants/api.ts API_BASE_URL）。
 */
import { afterEach, describe, expect, it } from 'vitest'
import {
  getServerBaseUrl,
  getShellApiBase,
  getShellAppVersion,
  refreshTokenStorageKey,
  resolveApiBase,
  SERVER_BASE_URL_KEY,
} from '../shellApiBase'

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG
  localStorage.removeItem(SERVER_BASE_URL_KEY)
})

describe('services/shellApiBase — getShellApiBase 分支', () => {
  it('壳未注入（浏览器/桌面）→ 空串', () => {
    expect(getShellApiBase()).toBe('')
  })

  it('注入合法 apiBase → 原样返回', () => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = {
      apiBase: 'http://162.14.69.153',
    }
    expect(getShellApiBase()).toBe('http://162.14.69.153')
  })

  it.each([
    ['apiBase 空串', { apiBase: '' }],
    ['apiBase 非字符串', { apiBase: 42 }],
    ['注入对象缺 apiBase 键', {}],
  ])('注入形状异常（%s）→ 空串兜底', (_label, cfg) => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = cfg
    expect(getShellApiBase()).toBe('')
  })
})

/* ── 连接基址优先级（连接设置页写入 server_base_url 后的解析契约）── */

describe('services/shellApiBase — resolveApiBase 优先级', () => {
  it('用户设置 > 壳注入', () => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = {
      apiBase: 'http://shell.example',
    }
    localStorage.setItem(SERVER_BASE_URL_KEY, 'http://user.example')
    expect(resolveApiBase()).toBe('http://user.example')
  })

  it('无用户设置 → 壳注入', () => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = {
      apiBase: 'http://shell.example',
    }
    expect(resolveApiBase()).toBe('http://shell.example')
  })

  it('用户设置为空白串视为未设置 → 壳注入', () => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = {
      apiBase: 'http://shell.example',
    }
    localStorage.setItem(SERVER_BASE_URL_KEY, '   ')
    expect(resolveApiBase()).toBe('http://shell.example')
  })

  it('无用户设置、无壳注入 → 空串（同源）', () => {
    expect(resolveApiBase()).toBe('')
  })

  it('getServerBaseUrl：未设置 → null；设置 → 原值', () => {
    expect(getServerBaseUrl()).toBeNull()
    localStorage.setItem(SERVER_BASE_URL_KEY, 'http://x.example/')
    expect(getServerBaseUrl()).toBe('http://x.example/')
  })

  it('getShellAppVersion：无壳 → null；壳注入版本面 → 原样', () => {
    expect(getShellAppVersion()).toBeNull()
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = {
      apiBase: 'http://shell.example',
      versionName: '1.4',
      versionCode: 5,
    }
    expect(getShellAppVersion()).toEqual({ versionName: '1.4', versionCode: 5 })
  })
})

/* ── refresh token 按后端分键（每后端登录态各自保存，切换往返不互串）── */

describe('services/shellApiBase — refreshTokenStorageKey 分键', () => {
  const shellBase = 'http://127.0.0.1:9300'

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG
    localStorage.removeItem(SERVER_BASE_URL_KEY)
  })

  it('默认档（无用户设置，壳注入）→ 裸键（本机内核模式行为不变）', () => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = { apiBase: shellBase }
    expect(refreshTokenStorageKey()).toBe('refresh_token')
  })

  it('同源模式（无壳、无用户设置，基址为空串）→ 裸键（浏览器/桌面不变）', () => {
    expect(refreshTokenStorageKey()).toBe('refresh_token')
  })

  it('自定义后端档（用户设置覆盖）→ 按基址分键', () => {
    ;(window as unknown as Record<string, unknown>).LINGXI_SHELL_CONFIG = { apiBase: shellBase }
    localStorage.setItem(SERVER_BASE_URL_KEY, 'http://192.168.1.10:9300')
    expect(refreshTokenStorageKey()).toBe('refresh_token@http://192.168.1.10:9300')
  })

  it('无壳但用户设置指向自定义后端 → 按基址分键（基址≠空串）', () => {
    localStorage.setItem(SERVER_BASE_URL_KEY, 'https://cloud.example.com')
    expect(refreshTokenStorageKey()).toBe('refresh_token@https://cloud.example.com')
  })

  it('不同后端基址 → 不同键（互不串写）', () => {
    const a = refreshTokenStorageKey('http://192.168.1.10:9300')
    const b = refreshTokenStorageKey('https://cloud.example.com')
    expect(a).not.toBe(b)
    expect(a.startsWith('refresh_token@')).toBe(true)
    expect(b.startsWith('refresh_token@')).toBe(true)
  })
})
