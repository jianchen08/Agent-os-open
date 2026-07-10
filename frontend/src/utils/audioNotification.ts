/**
 * 音频通知工具模块
 *
 * 使用 Web Audio API 合成短促的系统提示音，
 * 无需外部音频文件。
 *
 * Features:
 * - 使用 Web Audio API 合成 notification ping 音效
 * - 尊重用户偏好（检查 notification 权限和静音设置）
 * - 不阻塞主线程（异步播放）
 * - 自动处理浏览器的音频上下文限制
 */

/** 音频上下文单例（延迟初始化） */
let audioContext: AudioContext | null = null

/** 用户是否已经与页面交互过（浏览器要求） */
let userHasInteracted = false

/** 是否已初始化监听器 */
let interactionListenerInitialized = false

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
      const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext
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

/**
 * 使用 Web Audio API 合成短促的 notification ping 音效
 *
 * 音效参数：
 * - 频率: 880Hz (A5 音高，清脆悦耳)
 * - 波形: sine (正弦波，柔和)
 * - 时长: 150ms (短促不刺耳)
 * - 音量: 0.3 (适中音量)
 * - 衰减: 平滑淡出
 */
function synthesizeNotificationPing(ctx: AudioContext): void {
  const now = ctx.currentTime

  // 主音调振荡器
  const oscillator = ctx.createOscillator()
  const gainNode = ctx.createGain()

  oscillator.type = 'sine'
  oscillator.frequency.setValueAtTime(880, now)
  // 轻微的频率下滑，增加"叮"的感觉
  oscillator.frequency.exponentialRampToValueAtTime(660, now + 0.12)

  // 音量包络：快速起音 + 平滑淡出
  gainNode.gain.setValueAtTime(0, now)
  gainNode.gain.linearRampToValueAtTime(0.3, now + 0.01) // 快速起音
  gainNode.gain.exponentialRampToValueAtTime(0.001, now + 0.15) // 平滑淡出

  oscillator.connect(gainNode)
  gainNode.connect(ctx.destination)

  oscillator.start(now)
  oscillator.stop(now + 0.15)

  // 添加一个高频泛音，增加清脆感
  const harmonic = ctx.createOscillator()
  const harmonicGain = ctx.createGain()

  harmonic.type = 'sine'
  harmonic.frequency.setValueAtTime(1320, now)
  harmonic.frequency.exponentialRampToValueAtTime(990, now + 0.08)

  harmonicGain.gain.setValueAtTime(0, now)
  harmonicGain.gain.linearRampToValueAtTime(0.1, now + 0.005)
  harmonicGain.gain.exponentialRampToValueAtTime(0.001, now + 0.08)

  harmonic.connect(harmonicGain)
  harmonicGain.connect(ctx.destination)

  harmonic.start(now)
  harmonic.stop(now + 0.08)
}

/**
 * 播放交互请求通知音
 *
 * 使用 Web Audio API 合成短促的系统提示音。
 * 异步执行，不阻塞主线程。
 * 自动处理浏览器音频策略限制。
 *
 * @returns 是否成功播放
 */
export async function playNotificationSound(): Promise<boolean> {
  ensureInteractionListener()

  if (!shouldPlaySound()) return false

  try {
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

    synthesizeNotificationPing(ctx)
    return true
  } catch {
    // 静默处理播放失败，不影响主流程
    return false
  }
}

/**
 * 设置通知音静音状态
 *
 * @param muted - 是否静音
 */
export function setNotificationSoundMuted(muted: boolean): void {
  try {
    localStorage.setItem('notification_sound_muted', String(muted))
  } catch {
    // localStorage 不可用时静默处理
  }
}

/**
 * 获取通知音静音状态
 *
 * @returns 是否静音
 */
export function isNotificationSoundMuted(): boolean {
  try {
    return localStorage.getItem('notification_sound_muted') === 'true'
  } catch {
    return false
  }
}
