/** @feature FP-0.2.四 本机内核档凭据加载（shellLocalAuth 服务直测） | @ci: frontend-test */
/**
 * shellLocalAuth 服务契约：本机内核档凭据读取三态（桩缺失/凭据形状合法/
 * 桥抛错回落手动登录）+ 登录结果上报壳层（无桩静默跳过/成功上报/上报失败
 * 不影响主流程）+ 禁用旗标存储不可用回落 false。
 * 此前仅经 LoginModal mock 间接覆盖，本件补模块直测。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  loadShellLocalCredential,
  notifyShellLoginOutcome,
  shellAutoLoginDisabled,
} from '@/services/shellLocalAuth'

const apiBase = vi.hoisted(() => ({
  getServerBaseUrl: vi.fn((): string | null => null),
  getShellApiBase: vi.fn((): string | null => 'http://127.0.0.1:9300'),
}))

vi.mock('@/services/shellApiBase', () => ({
  getServerBaseUrl: (...args: unknown[]) => apiBase.getServerBaseUrl(...args),
  getShellApiBase: (...args: unknown[]) => apiBase.getShellApiBase(...args),
}))

const host = vi.hoisted(() => ({
  plugin: null as Record<string, unknown> | null,
}))

vi.mock('@/services/shellPlugins', () => ({
  getShellKernelHost: () => host.plugin,
}))


beforeEach(() => {
  sessionStorage.clear()
  apiBase.getServerBaseUrl.mockReturnValue(null)
  apiBase.getShellApiBase.mockReturnValue('http://127.0.0.1:9300')
  host.plugin = null
})

describe('loadShellLocalCredential 本机内核档凭据', () => {
  it('壳插件桩缺失（旧包 WebView）→ null 回落手动登录', async () => {
    host.plugin = null
    await expect(loadShellLocalCredential()).resolves.toBeNull()
    host.plugin = {}
    await expect(loadShellLocalCredential()).resolves.toBeNull()
  })

  it('凭据形状合法 → 返回 {username, password}', async () => {
    host.plugin = { localCredentials: vi.fn().mockResolvedValue({ username: 'admin', password: 'pw123' }) }
    await expect(loadShellLocalCredential()).resolves.toEqual({ username: 'admin', password: 'pw123' })
  })

  it('凭据字段缺失/空串 → null（不部分放行）', async () => {
    host.plugin = { localCredentials: vi.fn().mockResolvedValue({ username: 'admin', password: '' }) }
    await expect(loadShellLocalCredential()).resolves.toBeNull()
    host.plugin = { localCredentials: vi.fn().mockResolvedValue(undefined) }
    await expect(loadShellLocalCredential()).resolves.toBeNull()
  })

  it('桥调用抛错 → null 回落手动登录', async () => {
    host.plugin = { localCredentials: vi.fn().mockRejectedValue(new Error('bridge gone')) }
    await expect(loadShellLocalCredential()).resolves.toBeNull()
  })

  it('存量回环基址（手填 127.0.0.1:9300）豁免自定义后端闸 → 凭据照常返回', async () => {
    // 2026-10-23 用户令：填本机回环被判「自定义后端」→ 停本机栈+免填失效=死路；
    // 回环基址就是设备自己的内核，本机凭据必须继续可用。
    apiBase.getServerBaseUrl.mockReturnValue('http://127.0.0.1:9300')
    host.plugin = { localCredentials: vi.fn().mockResolvedValue({ username: 'admin', password: 'pw123' }) }
    await expect(loadShellLocalCredential()).resolves.toEqual({ username: 'admin', password: 'pw123' })
  })

  it('非回环自定义后端仍不给本机凭据（回归对照）', async () => {
    apiBase.getServerBaseUrl.mockReturnValue('http://203.0.113.153')
    host.plugin = { localCredentials: vi.fn().mockResolvedValue({ username: 'admin', password: 'pw123' }) }
    await expect(loadShellLocalCredential()).resolves.toBeNull()
  })
})

describe('notifyShellLoginOutcome 登录结果上报壳层', () => {
  it('无 reportLoginOutcome 桩（双版本共存）→ 静默跳过', async () => {
    host.plugin = {}
    await expect(notifyShellLoginOutcome(true)).resolves.toBeUndefined()
  })

  it('存量回环基址豁免自定义后端闸 → 看门狗计数面照常上报', async () => {
    apiBase.getServerBaseUrl.mockReturnValue('http://127.0.0.1:9300')
    const report = vi.fn().mockResolvedValue(undefined)
    host.plugin = { reportLoginOutcome: report }
    await notifyShellLoginOutcome(false)
    expect(report).toHaveBeenCalledWith({ success: false })
  })

  it('有桩 → 上报 success 载荷；上报失败被吞不阻塞', async () => {
    const report = vi.fn().mockResolvedValue(undefined)
    host.plugin = { reportLoginOutcome: report }
    await notifyShellLoginOutcome(false)
    expect(report).toHaveBeenCalledWith({ success: false })
    report.mockRejectedValue(new Error('x') as never)
    await expect(notifyShellLoginOutcome(true)).resolves.toBeUndefined()
  })

  it('凭据类失败 kind 过桥（原生类别闸执法依据：仅 credential 计数）', async () => {
    const report = vi.fn().mockResolvedValue(undefined)
    host.plugin = { reportLoginOutcome: report }
    await notifyShellLoginOutcome(false, 'credential')
    expect(report).toHaveBeenCalledWith({ success: false, kind: 'credential' })
    // 成功上报不携带类别（原生走复位路径，kind 无消费面）
    await notifyShellLoginOutcome(true)
    expect(report).toHaveBeenLastCalledWith({ success: true })
  })
})

describe('shellAutoLoginDisabled 禁用旗标', () => {
  it('sessionStorage 抛错 → 回落 false（无旗标即不禁用）', () => {
    const spy = vi.spyOn(sessionStorage, 'getItem').mockImplementation(() => {
      throw new Error('storage gone')
    })
    expect(shellAutoLoginDisabled()).toBe(false)
    spy.mockRestore()
  })
})
