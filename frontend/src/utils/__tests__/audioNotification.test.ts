// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * playNotificationSound 音频通知测试
 *
 * 覆盖：静音设置拦截、AudioContext 缺失、suspended 状态（已交互恢复/未交互拒绝）、
 * 正常播放合成音效、异常兜底 false。
 *
 * 注意：模块内 audioContext/userHasInteracted 是模块级缓存，每个用例
 * vi.resetModules() + 动态导入取全新模块实例（交互监听也随模块重建）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

/** 假 AudioContext：记录创建的节点与连接 */
function makeFakeAudioContext() {
  const nodes: any[] = []
  const ctx = {
    state: 'running',
    currentTime: 0,
    destination: { id: 'destination' },
    resume: vi.fn().mockResolvedValue(undefined),
    createOscillator: vi.fn(() => {
      const osc = {
        type: '',
        frequency: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
        connect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      }
      nodes.push(osc)
      return osc
    }),
    createGain: vi.fn(() => {
      const gain = {
        gain: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
        connect: vi.fn(),
      }
      nodes.push(gain)
      return gain
    }),
  }
  return { ctx, nodes }
}

let playNotificationSound: typeof import('@/utils/audioNotification').playNotificationSound

describe('playNotificationSound', () => {
  let originalAudioContext: any
  let originalWebkit: any

  beforeEach(async () => {
    originalAudioContext = (window as any).AudioContext
    originalWebkit = (window as any).webkitAudioContext
    localStorage.clear()
    vi.resetModules()
    playNotificationSound = (await import('@/utils/audioNotification')).playNotificationSound
  })

  afterEach(() => {
    ;(window as any).AudioContext = originalAudioContext
    ;(window as any).webkitAudioContext = originalWebkit
    localStorage.clear()
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('notification_sound_muted=true → 不创建 AudioContext，返回 false', async () => {
    localStorage.setItem('notification_sound_muted', 'true')
    const createSpy = vi.fn()
    ;(window as any).AudioContext = createSpy

    await expect(playNotificationSound()).resolves.toBe(false)
    expect(createSpy).not.toHaveBeenCalled()
  })

  it('AudioContext 不可用（缺失）→ 返回 false', async () => {
    ;(window as any).AudioContext = undefined
    await expect(playNotificationSound()).resolves.toBe(false)
  })

  it('AudioContext 构造抛错 → 返回 false', async () => {
    ;(window as any).AudioContext = class {
      constructor() { throw new Error('audio unsupported') }
    }
    await expect(playNotificationSound()).resolves.toBe(false)
  })

  it('正常路径（running）→ 合成主音+泛音振荡器并返回 true', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ;(window as any).AudioContext = class { constructor() { return ctx } }

    await expect(playNotificationSound()).resolves.toBe(true)
    const oscs = nodes.filter((n) => n.start && n.stop)
    const gains = nodes.filter((n) => n.gain)
    expect(oscs.length).toBe(2)
    expect(gains.length).toBe(2)
    expect(oscs[0].type).toBe('sine')
    expect(oscs[0].frequency.setValueAtTime).toHaveBeenCalledWith(880, 0)
    expect(oscs[0].connect).toHaveBeenCalled()
    expect(oscs[0].start).toHaveBeenCalledWith(0)
    expect(ctx.resume).not.toHaveBeenCalled()
  })

  it('suspended + 用户已交互 → resume 后播放返回 true', async () => {
    const { ctx } = makeFakeAudioContext()
    ctx.state = 'suspended'
    ;(window as any).AudioContext = class { constructor() { return ctx } }

    // 首次调用注册交互监听（此时未交互 → 返回 false）
    await expect(playNotificationSound()).resolves.toBe(false)
    // 用户点击后标记已交互
    document.dispatchEvent(new Event('click'))
    // 再次调用 → suspended 但已交互 → resume 后播放
    await expect(playNotificationSound()).resolves.toBe(true)
    expect(ctx.resume).toHaveBeenCalled()
  })

  it('suspended + 用户未交互 → 返回 false 不播放', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ctx.state = 'suspended'
    ;(window as any).AudioContext = class { constructor() { return ctx } }

    await expect(playNotificationSound()).resolves.toBe(false)
    expect(ctx.resume).not.toHaveBeenCalled()
    const oscs = nodes.filter((n) => n.start && n.stop)
    expect(oscs.length).toBe(0)
  })

  it('suspended + 已交互但 resume 失败 → 返回 false', async () => {
    const { ctx } = makeFakeAudioContext()
    ctx.state = 'suspended'
    ctx.resume = vi.fn().mockRejectedValue(new Error('resume denied'))
    ;(window as any).AudioContext = class { constructor() { return ctx } }

    await playNotificationSound() // 注册监听
    document.dispatchEvent(new Event('click'))
    await expect(playNotificationSound()).resolves.toBe(false)
  })
})
