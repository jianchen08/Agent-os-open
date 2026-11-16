// @feature: FP-0.2.通知音效 | @ci: frontend-test
/**
 * playNotificationSound 音频通知测试（插件递送目录 + 通用合成引擎形态）
 *
 * 覆盖：静音拦截、目录拉取失败/空目录降级、默认项解析（未选/未知 id 回落）、
 * 所选预设渲染、滑音+颤音 LFO、软混响节点面、suspended 三分支、选择偏好持久化。
 *
 * 注意：模块内 audioContext/userHasInteracted/catalogPromise 是模块级缓存，
 * 每个用例 vi.resetModules() + 动态导入取全新实例；目录服务 mock 随之重建，
 * 需在重置后重新动态导入以拿到当前实例。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { NotificationSoundPreset } from '@/services/api/notificationSounds'
import type * as audioNotificationMod from '@/utils/audioNotification'

vi.mock('@/services/api/notificationSounds', () => ({
  fetchNotificationSoundCatalog: vi.fn(),
}))

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
        frequency: {
          value: 0,
          setValueAtTime: vi.fn(),
          exponentialRampToValueAtTime: vi.fn(),
          connect: vi.fn(),
        },
        connect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      }
      nodes.push(osc)
      return osc
    }),
    createGain: vi.fn(() => {
      const gain = {
        gain: { value: 0, setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
        connect: vi.fn(),
      }
      nodes.push(gain)
      return gain
    }),
    createDelay: vi.fn(() => {
      const delay = { delayTime: { value: 0 }, connect: vi.fn() }
      nodes.push(delay)
      return delay
    }),
    createBiquadFilter: vi.fn(() => {
      const filter = { type: '', frequency: { value: 0 }, Q: { value: 0 }, connect: vi.fn() }
      nodes.push(filter)
      return filter
    }),
  }
  return { ctx, nodes }
}

const SIMPLE: NotificationSoundPreset = {
  id: 'simple',
  label: '简单音',
  default: true,
  params: {
    partials: [{ ratio: 1, gain: 1, decayRatio: 1 }],
    notes: [{ freq: 600, start: 0, peak: 0.3, decay: 0.4 }],
  },
}

const OTHER: NotificationSoundPreset = {
  id: 'other',
  label: '另一音',
  params: {
    partials: [{ ratio: 1, gain: 1, decayRatio: 1 }],
    notes: [
      { freq: 880, start: 0, peak: 0.3, decay: 0.4 },
      { freq: 660, start: 0.15, peak: 0.3, decay: 0.4 },
    ],
  },
}

/** 节点面更复杂的预设：泛音簇 × 2 音 + 滑音颤音 + 软混响 */
const RICH: NotificationSoundPreset = {
  id: 'rich',
  label: '复杂音',
  default: true,
  params: {
    partials: [
      { ratio: 1, gain: 1, decayRatio: 1 },
      { ratio: 2, gain: 0.22, decayRatio: 0.55 },
    ],
    attack: 0.01,
    wet: 0.5,
    taps: [
      { delay: 0.083, feedback: 0.42, lowpass: 5200 },
      { delay: 0.127, feedback: 0.36, lowpass: 3800 },
    ],
    notes: [
      { freq: 600, start: 0, peak: 0.3, decay: 0.4, glideTo: 1200, glideTime: 0.09, vibratoHz: 38, vibratoDepth: 0.03 },
      { freq: 800, start: 0.1, peak: 0.3, decay: 0.5 },
    ],
  },
}

type CatalogMock = { fetchNotificationSoundCatalog: ReturnType<typeof vi.fn> }
type Mod = typeof audioNotificationMod

async function loadModule(): Promise<[Mod, CatalogMock]> {
  const mod = (await import('@/utils/audioNotification')) as Mod
  const catalog = (await import('@/services/api/notificationSounds')) as unknown as CatalogMock
  return [mod, catalog]
}

describe('playNotificationSound', () => {
  let originalAudioContext: any
  let originalWebkit: any

  beforeEach(async () => {
    originalAudioContext = (window as any).AudioContext
    originalWebkit = (window as any).webkitAudioContext
    localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    ;(window as any).AudioContext = originalAudioContext
    ;(window as any).webkitAudioContext = originalWebkit
    localStorage.clear()
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('notification_sound_muted=true → 直接拒绝，不拉目录不建 AudioContext', async () => {
    localStorage.setItem('notification_sound_muted', 'true')
    const createSpy = vi.fn()
    ;(window as any).AudioContext = createSpy
    const [mod, catalog] = await loadModule()

    await expect(mod.playNotificationSound()).resolves.toBe(false)
    expect(catalog.fetchNotificationSoundCatalog).not.toHaveBeenCalled()
    expect(createSpy).not.toHaveBeenCalled()
  })

  it('目录拉取失败 → 返回 false 不抛错', async () => {
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockRejectedValue(new Error('plugin down'))

    await expect(mod.playNotificationSound()).resolves.toBe(false)
  })

  it('目录为空 → 返回 false', async () => {
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({ sounds: [], defaultId: '' })

    await expect(mod.playNotificationSound()).resolves.toBe(false)
  })

  it('未选择 → 渲染默认项预设（单音单泛音 → 单振荡器基频起振）', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE, OTHER],
      defaultId: 'simple',
    })

    await expect(mod.playNotificationSound()).resolves.toBe(true)
    const oscs = nodes.filter((n) => n.start && n.stop)
    expect(oscs.length).toBe(1)
    expect(oscs[0].type).toBe('sine')
    expect(oscs[0].frequency.setValueAtTime).toHaveBeenCalledWith(600, 0)
    expect(oscs[0].start).toHaveBeenCalledWith(0)
    // 干声预设（wet 缺省 0）不建混响节点
    expect(ctx.createDelay).not.toHaveBeenCalled()
    expect(ctx.createBiquadFilter).not.toHaveBeenCalled()
  })

  it('已选 other → 渲染所选预设（两音错峰），而非默认项', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE, OTHER],
      defaultId: 'simple',
    })
    mod.setNotificationSoundId('other')

    await expect(mod.playNotificationSound()).resolves.toBe(true)
    const oscs = nodes.filter((n) => n.start && n.stop)
    expect(oscs.length).toBe(2)
    expect(oscs[0].frequency.setValueAtTime).toHaveBeenCalledWith(880, 0)
    expect(oscs[1].frequency.setValueAtTime).toHaveBeenCalledWith(660, 0.15)
  })

  it('已选 id 不在目录 → 回落默认项', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE, OTHER],
      defaultId: 'simple',
    })
    mod.setNotificationSoundId('removed-sound')

    await expect(mod.playNotificationSound()).resolves.toBe(true)
    const oscs = nodes.filter((n) => n.start && n.stop)
    expect(oscs[0].frequency.setValueAtTime).toHaveBeenCalledWith(600, 0)
  })

  it('泛音簇×滑音×颤音×软混响：振荡器含 LFO，频率指数滑音，混响双抽头四延迟', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [RICH],
      defaultId: 'rich',
    })

    await expect(mod.playNotificationSound()).resolves.toBe(true)
    const oscs = nodes.filter((n) => n.start && n.stop)
    // 2 音 × 2 泛音 + 首音每泛音各 1 个颤音 LFO（×2）= 6
    expect(oscs.length).toBe(6)
    expect(oscs[0].frequency.exponentialRampToValueAtTime).toHaveBeenCalledWith(1200, 0.09)
    // 起音包络：0 → 0.01s 线性到峰值，再指数衰减（按行为定位音符增益节点）
    const gains = nodes.filter((n) => n.gain)
    const noteGain = gains.find((g) =>
      g.gain.linearRampToValueAtTime.mock.calls.some((call: unknown[]) => call[0] === 0.3),
    )
    expect(noteGain?.gain.linearRampToValueAtTime).toHaveBeenCalledWith(0.3, 0.01)
    // 混响：2 抽头 × (入延迟 + 反馈循环延迟)
    expect(ctx.createDelay).toHaveBeenCalledTimes(4)
    expect(ctx.createBiquadFilter).toHaveBeenCalledTimes(2)
    expect(oscs[1].connect).toHaveBeenCalled() // LFO 接入主振荡器 frequency
  })

  it('suspended + 用户已交互 → resume 后播放返回 true', async () => {
    const { ctx } = makeFakeAudioContext()
    ctx.state = 'suspended'
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE],
      defaultId: 'simple',
    })

    // 首次调用注册交互监听（此时未交互 → 返回 false）
    await expect(mod.playNotificationSound()).resolves.toBe(false)
    // 用户点击后标记已交互
    document.dispatchEvent(new Event('click'))
    // 再次调用 → suspended 但已交互 → resume 后播放
    await expect(mod.playNotificationSound()).resolves.toBe(true)
    expect(ctx.resume).toHaveBeenCalled()
  })

  it('suspended + 用户未交互 → 返回 false 不播放', async () => {
    const { ctx, nodes } = makeFakeAudioContext()
    ctx.state = 'suspended'
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE],
      defaultId: 'simple',
    })

    await expect(mod.playNotificationSound()).resolves.toBe(false)
    expect(ctx.resume).not.toHaveBeenCalled()
    const oscs = nodes.filter((n) => n.start && n.stop)
    expect(oscs.length).toBe(0)
  })

  it('suspended + 已交互但 resume 失败 → 返回 false', async () => {
    const { ctx } = makeFakeAudioContext()
    ctx.state = 'suspended'
    ctx.resume = vi.fn().mockRejectedValue(new Error('resume denied'))
    ;(window as any).AudioContext = class {
      constructor() {
        return ctx
      }
    }
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE],
      defaultId: 'simple',
    })

    await mod.playNotificationSound() // 注册监听
    document.dispatchEvent(new Event('click'))
    await expect(mod.playNotificationSound()).resolves.toBe(false)
  })

  it('AudioContext 不可用/构造抛错 → 返回 false', async () => {
    const [mod, catalog] = await loadModule()
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE],
      defaultId: 'simple',
    })
    ;(window as any).AudioContext = undefined
    await expect(mod.playNotificationSound()).resolves.toBe(false)

    vi.resetModules()
    const [mod2, catalog2] = await loadModule()
    catalog2.fetchNotificationSoundCatalog.mockResolvedValue({
      sounds: [SIMPLE],
      defaultId: 'simple',
    })
    ;(window as any).AudioContext = class {
      constructor() {
        throw new Error('audio unsupported')
      }
    }
    await expect(mod2.playNotificationSound()).resolves.toBe(false)
  })
})

describe('notification sound selection preference', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('set/get 往返；空串清除', async () => {
    const [mod] = await loadModule()
    expect(mod.getNotificationSoundId()).toBe('')
    mod.setNotificationSoundId('dingdong_down')
    expect(mod.getNotificationSoundId()).toBe('dingdong_down')
    expect(localStorage.getItem(mod.NOTIFICATION_SOUND_STORAGE_KEY)).toBe('dingdong_down')
    mod.setNotificationSoundId('')
    expect(mod.getNotificationSoundId()).toBe('')
    expect(localStorage.getItem(mod.NOTIFICATION_SOUND_STORAGE_KEY)).toBeNull()
  })

  it('localStorage 写入抛错 → 静默吞掉（选择仅当次生效）', async () => {
    const setSpy = vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    const [mod] = await loadModule()
    expect(() => mod.setNotificationSoundId('x')).not.toThrow()
    expect(setSpy).toHaveBeenCalledWith(mod.NOTIFICATION_SOUND_STORAGE_KEY, 'x')
  })
})
