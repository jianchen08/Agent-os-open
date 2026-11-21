/**
 * @feature FP-T12 前端组件补测 | @ci: frontend-test
 * useVoiceInput 缺口补测：能力不可用早退 / 能力中途消失 / 到时自动停 / preferBrowser 降级。
 *
 * 行为契约（useVoiceInput.ts）：
 *  - startServerASR 在 MediaRecorder 不可用时报 not_supported（'当前设备不支持录音'）；
 *  - 浏览器识别构造器在起录间隙消失（渲染时在、调用时没了）→ initSpeechRecognition
 *    报 not_supported（'当前浏览器不支持语音识别'）；
 *  - 浏览器识别录音达到 maxDurationSeconds 自动触发 stopRecording（到时停表）；
 *  - preferBrowser 且无识别能力时 startRecording 落到服务端 ASR 主通道。
 * MediaRecorder/getUserMedia/SpeechRecognition 均为宿主浏览器能力，按外部依赖桩替。
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
const { getShellSpeechMock } = vi.hoisted(() => ({ getShellSpeechMock: vi.fn<() => unknown>(() => null) }))
vi.mock('@/services/shellPlugins', () => ({ getShellSpeech: getShellSpeechMock }))
import { useVoiceInput } from '../useVoiceInput'
import type { SpeechRecognitionInstance } from '@/types/voiceInput'

/** 最小 SpeechRecognition 替身：可构造、可 start/stop、回调槽位可赋值 */
class FakeSpeechRecognition implements Partial<SpeechRecognitionInstance> {
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: unknown) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn()
  abort = vi.fn()
}

/** 最小 MediaRecorder 替身：静态 isTypeSupported 支持探测，实例记录 start/stop */
class FakeMediaRecorder {
  static isTypeSupported = vi.fn(() => true)
  state = 'inactive'
  ondataavailable: ((e: unknown) => void) | null = null
  onstop: (() => void) | null = null
  start = vi.fn(() => {
    this.state = 'recording'
  })
  stop = vi.fn(() => {
    this.state = 'inactive'
  })
  constructor(public stream: unknown, _options?: unknown) {}
}

function stubGetUserMedia() {
  const trackStop = vi.fn()
  const stream = { getTracks: () => [{ stop: trackStop }] }
  const getUserMedia = vi.fn(async () => stream)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true,
  })
  return { getUserMedia, trackStop }
}

beforeEach(() => {
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useVoiceInput 能力缺口', () => {
  it('MediaRecorder 不可用时 startServerASR 报 not_supported 早退', async () => {
    vi.unstubAllGlobals() // jsdom 默认无 MediaRecorder
    const { result } = renderHook(() => useVoiceInput())
    expect(result.current.isMediaRecorderSupported).toBe(false)
    await act(async () => {
      await result.current.startServerASR()
    })
    expect(result.current.error).toEqual({
      type: 'not_supported',
      message: '当前设备不支持录音',
    })
    expect(result.current.state).toBe('idle')
    expect(result.current.mode).toBe('browser') // 早退前不切 server-asr
  })

  it('识别构造器在起录间隙消失 → initSpeechRecognition 报 not_supported', async () => {
    const w = window as unknown as Record<string, unknown>
    w.SpeechRecognition = FakeSpeechRecognition
    const { result } = renderHook(() =>
      useVoiceInput({ preferBrowser: true, continuous: false }),
    )
    expect(result.current.isSpeechRecognitionSupported).toBe(true)
    // 渲染后在调用前能力消失（真实场景：WebView 内核降级/扩展移除 API）
    act(() => {
      delete w.SpeechRecognition
    })
    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.error).toEqual({
      type: 'not_supported',
      message: '当前浏览器不支持语音识别',
    })
    expect(result.current.state).toBe('idle')
  })

  it('preferBrowser 且无识别能力 → startRecording 落服务端 ASR 主通道', async () => {
    const { getUserMedia } = stubGetUserMedia()
    const { result } = renderHook(() =>
      useVoiceInput({ preferBrowser: true, continuous: false }),
    )
    expect(result.current.isSpeechRecognitionSupported).toBe(false)
    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.state).toBe('recording')
    expect(result.current.mode).toBe('server-asr')
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
  })
})

describe('useVoiceInput 到时自动停止（浏览器识别通道）', () => {
  it('达到 maxDurationSeconds 触发 stopRecording：识别 stop 被调、状态回 idle', async () => {
    vi.useFakeTimers()
    const w = window as unknown as Record<string, unknown>
    w.SpeechRecognition = FakeSpeechRecognition
    const { result } = renderHook(() =>
      useVoiceInput({ preferBrowser: true, continuous: false, maxDurationSeconds: 1 }),
    )
    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.state).toBe('recording')
    expect(result.current.mode).toBe('browser')

    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(result.current.recordingDuration).toBe(1)
    expect(result.current.state).toBe('idle')
    expect(result.current.error).toBeNull()
  })
})


describe('useVoiceInput — 原生识别桥回退面', () => {
  beforeEach(() => {
    getShellSpeechMock.mockReset()
    getShellSpeechMock.mockReturnValue(null)
  })

  it('原生桥在起录间隙消失 → 按「原生不可用」语义回退服务端 ASR（不吞错）', async () => {
    stubGetUserMedia()
    // 渲染面与 startRecording 判定面桥在场 → 走原生识别入口
    getShellSpeechMock.mockReturnValue({ startNativeCapture: vi.fn() })
    const { result } = renderHook(() => useVoiceInput())
    // 入口内再取桥时桥已消失（能力竞态）→ 回退服务端 ASR 主通道
    getShellSpeechMock.mockReturnValue(null)

    await act(async () => {
      await result.current.startRecording({ preferBrowser: false })
    })

    await waitFor(() => expect(result.current.isRecording).toBe(true))
    expect(result.current.error).toBeNull()
  })
})
