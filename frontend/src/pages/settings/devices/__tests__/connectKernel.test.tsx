/** @feature FP-0.2.多设备目录树 连接内核探活闸 | @ci: frontend-test */
// @ci: frontend-test
/**
 * 连接内核动作探活闸测试（2026-10-22 用户实测三撞：死目标把应用切进不可自救
 * 的远端登录闸）。契约：
 *  - 探活失败 → 不写 server_base_url、不上报后端模式、不刷新，返回错误文案；
 *  - 探活通过 → 写设置 + 上报 remote + 定时刷新收尾；
 *  - 空地址（切回本机档）→ 不探活直接清除设置（壳冷启动窗口内探活必假阴）；
 *  - 回环地址（127.x / localhost / ::1）归并本机档——同一不探活/清除/local 路径
 *    （消灭「填 127.0.0.1:9300 被判自定义后端 → 停本机栈+本机免填凭据失效」死路）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connectKernel } from '../connectKernel'

const probeMock = vi.hoisted(() => ({ probeConnection: vi.fn() }))
const shellMode = vi.hoisted(() => ({ applyShellBackendMode: vi.fn() }))

vi.mock('@/services/connectionProbe', () => ({
  probeConnection: (...args: unknown[]) => probeMock.probeConnection(...args),
  validateServerBaseUrl: vi.fn((input: string) => {
    const trimmed = input.trim().replace(/\/+$/, '')
    if (!trimmed) return { ok: true, url: null }
    if (/^https?:\/\//i.test(trimmed)) return { ok: true, url: trimmed }
    return { ok: true, url: `http://${trimmed}` }
  }),
  isLoopbackOrigin: (origin: string) => {
    try {
      return /^(localhost$|127\.\d+\.\d+\.\d+$|::1$|\[::1\]$)/.test(new URL(origin).hostname.toLowerCase())
    } catch {
      return false
    }
  },
}))
vi.mock('@/services/shellBackendMode', () => shellMode)


describe('connectKernel 探活闸', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('探活失败：拒绝切换（不写设置/不上报/不刷新），返回错误文案', async () => {
    probeMock.probeConnection.mockResolvedValue({ ok: false, status: 0, kernelVersion: null, latencyMs: 40 })

    const r = await connectKernel('http://10.0.0.8:9100')

    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toContain('目标内核不可达')
    expect(localStorage.getItem('server_base_url')).toBeNull()
    expect(shellMode.applyShellBackendMode).not.toHaveBeenCalled()
  })

  it('探活通过：写设置 + 上报 remote + 刷新收尾', async () => {
    probeMock.probeConnection.mockResolvedValue({ ok: true, status: 200, kernelVersion: '0.2.2', latencyMs: 30 })

    const r = await connectKernel('http://203.0.113.153')

    expect(r.ok).toBe(true)
    expect(localStorage.getItem('server_base_url')).toBe('http://203.0.113.153')
    expect(shellMode.applyShellBackendMode).toHaveBeenCalledWith('remote')
    expect(probeMock.probeConnection).toHaveBeenCalledWith('http://203.0.113.153')
  })

  it('空地址切回本机档：不探活，清除设置并上报 local', async () => {
    localStorage.setItem('server_base_url', 'http://10.0.0.8:9100')

    const r = await connectKernel('')

    expect(r.ok).toBe(true)
    expect(probeMock.probeConnection).not.toHaveBeenCalled()
    expect(localStorage.getItem('server_base_url')).toBeNull()
    expect(shellMode.applyShellBackendMode).toHaveBeenCalledWith('local')
  })

  it('回环地址归并本机档：不探活、清设置、上报 local（停本机栈死路根除）', async () => {
    const r = await connectKernel('http://127.0.0.1:9300')

    expect(r.ok).toBe(true)
    expect(probeMock.probeConnection).not.toHaveBeenCalled()
    expect(localStorage.getItem('server_base_url')).toBeNull()
    expect(shellMode.applyShellBackendMode).toHaveBeenCalledWith('local')
  })

  it('localhost 形态同样归并本机档（无协议输入补 http 后仍识别）', async () => {
    const r = await connectKernel('localhost:9300')

    expect(r.ok).toBe(true)
    expect(probeMock.probeConnection).not.toHaveBeenCalled()
    expect(localStorage.getItem('server_base_url')).toBeNull()
    expect(shellMode.applyShellBackendMode).toHaveBeenCalledWith('local')
  })

  it('非回环远端不受归并影响：照常探活 + remote（回归对照）', async () => {
    probeMock.probeConnection.mockResolvedValue({ ok: true, status: 200, kernelVersion: '0.2.2', latencyMs: 30 })

    const r = await connectKernel('http://192.168.1.10:9300')

    expect(r.ok).toBe(true)
    expect(probeMock.probeConnection).toHaveBeenCalledWith('http://192.168.1.10:9300')
    expect(localStorage.getItem('server_base_url')).toBe('http://192.168.1.10:9300')
    expect(shellMode.applyShellBackendMode).toHaveBeenCalledWith('remote')
  })
})
