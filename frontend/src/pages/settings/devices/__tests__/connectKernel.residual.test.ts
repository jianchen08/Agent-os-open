/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * connectKernel（连接切换唯一写点）的两条拒绝路径：
 * - 地址协议不受支持 → 直接返回校验原因（不写存储、不做原生动作）
 * - 本地存储不可用 → 返回显式错误（不假成功、不刷新页面）
 *
 * mock 边界：连接探活（网络）、壳后端模式上报（原生壳能力）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connectKernel } from '../connectKernel'

const probeMock = vi.hoisted(() => ({ probeConnection: vi.fn() }))
vi.mock('@/services/connectionProbe', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/connectionProbe')>()
  return {
    ...actual,
    probeConnection: (...args: unknown[]) => probeMock.probeConnection(...args),
  }
})

const applyShellBackendModeMock = vi.hoisted(() => vi.fn())
vi.mock('@/services/shellBackendMode', () => ({
  applyShellBackendMode: (...args: unknown[]) => applyShellBackendModeMock(...args),
}))

const reloadMock = vi.hoisted(() => vi.fn())
Object.defineProperty(window, 'location', {
  value: { ...window.location, reload: reloadMock },
  writable: true,
})

beforeEach(() => {
  localStorage.clear()
  probeMock.probeConnection.mockReset().mockResolvedValue({ ok: true, status: 200, latencyMs: 3 })
  applyShellBackendModeMock.mockReset()
  reloadMock.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('connectKernel — 拒绝路径', () => {
  it('不支持的协议 → 返回校验原因，不写存储不刷新', async () => {
    const result = await connectKernel('ftp://192.168.1.30:9300')

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error).toContain('仅支持 http/https')
    expect(localStorage.getItem('server_base_url')).toBeNull()
    expect(reloadMock).not.toHaveBeenCalled()
  })

  it('本地存储不可用 → 显式失败（不假成功、无原生动作与刷新）', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => {
        throw new Error('QuotaExceededError')
      },
      removeItem: () => {},
    })

    const result = await connectKernel('http://192.168.1.30:9300')

    expect(result).toEqual({ ok: false, error: '本地存储不可用，未切换连接' })
    expect(applyShellBackendModeMock).not.toHaveBeenCalled()
    expect(reloadMock).not.toHaveBeenCalled()
  })
})
