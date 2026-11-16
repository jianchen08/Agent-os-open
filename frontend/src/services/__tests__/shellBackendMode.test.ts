/** @feature FP-0.2.四 后端模式上报壳层（applyShellBackendMode/reconcile 直测） | @ci: frontend-test */
/**
 * applyShellBackendMode 契约：桩缺失静默跳过；有桩透传模式载荷；桥失败
 * 吞掉不阻塞保存流程（冷启动按存储值分流，下次保存自愈）。
 * reconcileShellBackendMode 契约（2026-10-23 真机撞墙归一）：启动期按连接目标
 * 归位持久化模式——本机档（无自定义基址/回环基址）→ local（残留 remote 会让
 * 本机栈永不拉起=127.0.0.1 拒连）；非回环自定义基址 → remote；无壳桩不调用。
 */
import { describe, expect, it, vi } from 'vitest'

const host = vi.hoisted(() => ({ plugin: null as Record<string, unknown> | null }))

vi.mock('@/services/shellPlugins', () => ({
  getShellKernelHost: () => host.plugin,
}))

const apiBase = vi.hoisted(() => ({ serverBaseUrl: null as string | null }))

vi.mock('@/services/shellApiBase', () => ({
  getServerBaseUrl: () => apiBase.serverBaseUrl,
}))

vi.mock('@/services/connectionProbe', () => ({
  isLoopbackOrigin: (origin: string) => {
    try {
      return /^(localhost$|127\.\d+\.\d+\.\d+$|::1$|\[::1\]$)/.test(new URL(origin).hostname.toLowerCase())
    } catch {
      return false
    }
  },
}))

import { applyShellBackendMode, reconcileShellBackendMode } from '@/services/shellBackendMode'

describe('applyShellBackendMode 后端模式上报', () => {
  it('壳无 KernelHost 桩（浏览器/桌面）→ 静默跳过', () => {
    host.plugin = null
    expect(() => applyShellBackendMode('remote')).not.toThrow()
  })

  it('有桩 → 透传模式载荷；桥失败被吞不抛错', async () => {
    const setBackendMode = vi.fn().mockResolvedValue(undefined)
    host.plugin = { setBackendMode }
    applyShellBackendMode('remote')
    expect(setBackendMode).toHaveBeenCalledWith({ mode: 'remote' })
    setBackendMode.mockRejectedValue(new Error('x') as never)
    expect(() => applyShellBackendMode('local')).not.toThrow()
    // 微任务冲账，避免 reject 落到测试框架的未处理拒绝面
    await new Promise((r) => setTimeout(r, 0))
  })
})

describe('reconcileShellBackendMode 启动期模式归位', () => {
  it('无自定义基址（本机档）→ 归位 local', () => {
    apiBase.serverBaseUrl = null
    const setBackendMode = vi.fn().mockResolvedValue(undefined)
    host.plugin = { setBackendMode }
    reconcileShellBackendMode()
    expect(setBackendMode).toHaveBeenCalledWith({ mode: 'local' })
  })

  it('存量回环基址（旧版误存自定义后端）→ 归位 local（本机栈拉起，病灶修复）', () => {
    apiBase.serverBaseUrl = 'http://127.0.0.1:9300'
    const setBackendMode = vi.fn().mockResolvedValue(undefined)
    host.plugin = { setBackendMode }
    reconcileShellBackendMode()
    expect(setBackendMode).toHaveBeenCalledWith({ mode: 'local' })
  })

  it('非回环自定义基址 → 归位 remote（栈生命周期与目标一致）', () => {
    apiBase.serverBaseUrl = 'http://162.14.69.153'
    const setBackendMode = vi.fn().mockResolvedValue(undefined)
    host.plugin = { setBackendMode }
    reconcileShellBackendMode()
    expect(setBackendMode).toHaveBeenCalledWith({ mode: 'remote' })
  })

  it('壳无 KernelHost 桩（浏览器/桌面）→ 不调用', () => {
    apiBase.serverBaseUrl = null
    host.plugin = null
    expect(() => reconcileShellBackendMode()).not.toThrow()
  })
})
