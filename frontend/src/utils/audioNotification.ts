/**
 * 音频通知工具模块——提示音渲染器（数据全部由插件递送，前端零音效硬编码）
 *
 * 音效目录来自 notification_sounds 插件（GET /ext/notification_sounds/sounds，
 * sounds/*.json 即内容，用户/agent 落预设文件即上目录）；本模块只承载：
 * - Web Audio 通用合成引擎：泛音簇振荡器 + 指数滑音 + 颤音 LFO + 反馈延迟软混响
 * - 音效选择偏好（localStorage `notification_sound_id`，设备级，与
 *   `notification_sound_muted` 静音开关同一存放模式）
 * - 播放决策：静音/目录不可用/AudioContext 不可用均静默跳过（返回 false，
 *   调用方已有应用内通知降级路径）
 */

import {
  fetchNotificationSoundCatalog,
  type NotificationSoundPreset,
  type SoundSynthParams,
} from '@/services/api/notificationSounds'

/** 音效选择偏好存储键 */
export const NOTIFICATION_SOUND_STORAGE_KEY = 'notification_sound_id'

/** 音频上下文单例（延迟初始化） */
let audioContext: AudioContext | null = null

/** 用户是否已经与页面交互过（浏览器要求） */
let userHasInteracted = false

/** 是否已初始化监听器 */
let interactionListenerInitialized = false

/** 目录缓存（含在飞去重）；失败不缓存，下次播放重试 */
let catalogPromise: Promise<{ sounds: NotificationSoundPreset[]; defaultId: string }> | null = null

/**
 * 初始化用户交互监听
 *
 * 浏览器安全策略要求用户先与页面交互后才能播放音频。
 * 监听第一次用户交互（click/keydown/touch）后标记为可播放。
 */
function ensureInteractionListener(): void {
  if (interactionListenerInitialized || typeof window === 'undefined') return
  interactionListenerInitialized = true

  const markInteracted = () => {
    userHasInteracted = true
    // 首次交互后恢复 AudioContext（如果处于 suspended 状态）
    if (audioContext?.state === 'suspended') {
      audioContext.resume().catch(() => {
        // 静默处理恢复失败
      })
    }
  }

  const events = ['click', 'keydown', 'touchstart'] as const
  events.forEach((event) => {
    document.addEventListener(event, markInteracted, { once: true, passive: true })
  })
}

/**
 * 获取或创建 AudioContext
 *
 * 延迟初始化，避免在页面加载时创建音频上下文。
 * 复用同一个 AudioContext 以节省资源。
 */
function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined') return null

  try {
    if (!audioContext) {
      // 旧 Safari 前缀形态（webkitAudioContext）：类型化为可选构造器而非 any
      const legacyWindow = window as Window & { webkitAudioContext?: typeof AudioContext }
      const AudioContextClass = window.AudioContext || legacyWindow.webkitAudioContext
      if (!AudioContextClass) return null
      audioContext = new AudioContextClass()
    }
    return audioContext
  } catch {
    return null
  }
}

/**
 * 检查是否应该播放通知音
 *
 * 尊重用户偏好：
 * 1. 检查 localStorage 中的静音设置
 * 2. 检查 Notification API 权限（如果可用）
 */
function shouldPlaySound(): boolean {
  if (typeof window === 'undefined') return false

  // 检查用户是否设置了静音
  try {
    const muted = localStorage.getItem('notification_sound_muted')
    if (muted === 'true') return false
  } catch {
    // localStorage 不可用时继续播放
  }

  return true
}

async function getCatalog(): Promise<{ sounds: NotificationSoundPreset[]; defaultId: string }> {
  if (!catalogPromise) {
    catalogPromise = fetchNotificationSoundCatalog().catch((error: unknown) => {
      catalogPromise = null // 失败不缓存：下次播放重试拉取
      throw error
    })
  }
  return catalogPromise
}

/** 读取音效选择偏好；未选择返回空串（由 resolvePreset 回落默认项） */
export function getNotificationSoundId(): string {
  if (typeof window === 'undefined') return ''
  try {
    return localStorage.getItem(NOTIFICATION_SOUND_STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

/** 持久化音效选择；空串清除（回落插件默认项）。存储不可用静默。 */
export function setNotificationSoundId(id: string): void {
  if (typeof window === 'undefined') return
  try {
    if (id) localStorage.setItem(NOTIFICATION_SOUND_STORAGE_KEY, id)
    else localStorage.removeItem(NOTIFICATION_SOUND_STORAGE_KEY)
  } catch {
    // 存储不可用时静默：选择仅当次生效
  }
}

/**
 * 拉取目录供设置页等消费方枚举音效；失败折算空目录（展示"不可用"）。
 */
export async function getNotificationSoundOptions(): Promise<{
  sounds: NotificationSoundPreset[]
  defaultId: string
}> {
  try {
    return await getCatalog()
  } catch {
    return { sounds: [], defaultId: '' }
  }
}

/** 依偏好解析待渲染预设：所选 id 命中 → 默认项 → 目录首个；目录不可用 → null */
async function resolvePreset(): Promise<SoundSynthParams | null> {
  try {
    const catalog = await getCatalog()
    if (catalog.sounds.length === 0) return null
    const stored = getNotificationSoundId()
    const picked =
      catalog.sounds.find((s) => s.id === stored) ??
      catalog.sounds.find((s) => s.id === catalog.defaultId) ??
      catalog.sounds[0]
    return picked?.params ?? null
  } catch {
    return null
  }
}

/**
 * 通用合成引擎：按预设参数渲染
 *
 * 结构（与预设 JSON 契约一一对应）：
 * - 每音 × 每泛音成分一个正弦振荡器，attack 起音后指数衰减（duration = decay × decayRatio）
 * - 音符可选指数滑音（glideTo/glideTime）与颤音 LFO（vibratoHz/vibratoDepth）
 * - wet>0 时经反馈延迟软混响（每抽头：延迟 → 低通 → 汇总，汇总反馈回延迟）
 */
function synthesizePreset(ctx: AudioContext, params: SoundSynthParams): void {
  const now = ctx.currentTime
  const attack = params.attack ?? 0.006
  const wet = params.wet ?? 0
  const taps = params.taps ?? []

  const master = ctx.createGain()
  master.gain.value = 0.85
  master.connect(ctx.destination)

  const tapInputs: GainNode[] = []
  if (wet > 0 && taps.length > 0) {
    for (const tap of taps) {
      const input = ctx.createGain()
      input.gain.value = wet / taps.length
      const delay = ctx.createDelay(1)
      delay.delayTime.value = tap.delay
      const lowpass = ctx.createBiquadFilter()
      lowpass.type = 'lowpass'
      lowpass.frequency.value = tap.lowpass
      const feedback = ctx.createGain()
      feedback.gain.value = tap.feedback
      const recirc = ctx.createDelay(1)
      recirc.delayTime.value = tap.delay
      const sum = ctx.createGain()

      input.connect(delay)
      delay.connect(lowpass)
      lowpass.connect(sum)
      sum.connect(feedback)
      feedback.connect(recirc)
      recirc.connect(sum)
      sum.connect(master)

      tapInputs.push(input)
    }
  }

  for (const noteDef of params.notes) {
    const t0 = now + noteDef.start
    for (const partial of params.partials) {
      const peak = noteDef.peak * partial.gain
      if (peak <= 0) continue // 指数包络起点必须为正
      const duration = noteDef.decay * partial.decayRatio

      const osc = ctx.createOscillator()
      const gain = ctx.createGain()

      osc.type = 'sine'
      osc.frequency.setValueAtTime(noteDef.freq * partial.ratio, t0)
      if (noteDef.glideTo != null && noteDef.glideTime != null) {
        osc.frequency.exponentialRampToValueAtTime(
          noteDef.glideTo * partial.ratio,
          t0 + noteDef.glideTime,
        )
      }
      if (noteDef.vibratoHz != null && noteDef.vibratoDepth != null) {
        const lfo = ctx.createOscillator()
        const lfoGain = ctx.createGain()
        lfo.frequency.value = noteDef.vibratoHz
        lfoGain.gain.value = noteDef.freq * noteDef.vibratoDepth * partial.ratio
        lfo.connect(lfoGain)
        lfoGain.connect(osc.frequency)
        lfo.start(t0)
        lfo.stop(t0 + duration)
      }

      gain.gain.setValueAtTime(0, t0)
      gain.gain.linearRampToValueAtTime(peak, t0 + attack)
      gain.gain.exponentialRampToValueAtTime(Math.max(peak * 0.001, 0.0001), t0 + duration)

      osc.connect(gain)
      gain.connect(master)
      for (const tapInput of tapInputs) gain.connect(tapInput)

      osc.start(t0)
      osc.stop(t0 + duration)
    }
  }
}

/**
 * 播放交互请求通知音
 *
 * 按用户所选预设（未选则插件默认项）经通用引擎渲染。
 * 异步执行，不阻塞主线程；自动处理浏览器音频策略限制。
 *
 * @returns 是否成功播放
 */
export async function playNotificationSound(): Promise<boolean> {
  ensureInteractionListener()

  if (!shouldPlaySound()) return false

  try {
    const params = await resolvePreset()
    if (!params) return false

    const ctx = getAudioContext()
    if (!ctx) return false

    // 如果 AudioContext 被挂起（浏览器限制），尝试恢复
    if (ctx.state === 'suspended') {
      if (userHasInteracted) {
        await ctx.resume()
      } else {
        // 用户尚未交互，无法播放
        return false
      }
    }

    synthesizePreset(ctx, params)
    return true
  } catch {
    // 静默处理播放失败，不影响主流程
    return false
  }
}
