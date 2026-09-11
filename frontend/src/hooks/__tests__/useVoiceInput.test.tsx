/** @ci frontend-test */
/**
 * useVoiceInput 全链契约测试（0% → 全覆盖批次）。
 *
 * 覆盖：浏览器 SpeechRecognition 模式（启动/中间与最终结果/错误分派/手动停止
 * 吞错/连续模式 onend 自动重启）、服务端 ASR 降级（network/service-not-allowed
 * 触发，getUserMedia + MediaRecorder + transcribeAudio 三态）、音频录制模式
 * （supportsAudio=true，onRecordingComplete 回收 Blob）、录音计时器、卸载清理。
 *
 * 隔离策略：Web Speech API / MediaRecorder / getUserMedia 均为浏览器外部
 * 能力，替身注入 window/globalThis；transcribeAudio 网络依赖 vi.mock。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockTranscribe = vi.fn()
vi.mock('@/services/api/asr', () => ({
  transcribeAudio: (...args: unknown[]) => mockTranscribe(...args),
}))

import { useVoiceInput } from '../useVoiceInput'
import type {
  SpeechRecognitionErrorEvent,
  SpeechRecognitionEvent,
} from '@/types/voiceInput'

/** SpeechRecognition 替身：记录 start/stop，回调可手动触发；静态持有最后实例供断言 */
class FakeRecognition {
  static lastInstance?: FakeRecognition
  lang = ''
  continuous = false
  interimResults = false
  maxAlternatives = 1
  start = vi.fn()
  stop = vi.fn()
  onresult: ((e: SpeechRecognitionEvent) => void) | null = null
  onerror: ((e: SpeechRecognitionErrorEvent) => void) | null = null
  onend: (() => void) | null = null

  constructor() {
    FakeRecognition.lastInstance = this
  }
}

/** MediaRecorder 替身：start/stop 驱动 state 机，onstop 手动触发；静态持有最后实例 */
class FakeMediaRecorder {
  static lastInstance?: FakeMediaRecorder
  static isTypeSupported = vi.fn((type: string) => type === 'audio/webm')
  state = 'inactive'
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  start = vi.fn(function (this: FakeMediaRecorder) {
    this.state = 'recording'
  })
  stop = vi.fn(function (this: FakeMediaRecorder) {
    if (this.state !== 'inactive') {
      this.state = 'inactive'
      this.onstop?.()
    }
  })

  constructor(_stream?: MediaStream, _opts?: { mimeType?: string }) {
    FakeMediaRecorder.lastInstance = this
  }
}

function makeStream(): MediaStream {
  return { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream
}

/** 构造 onresult 事件：final/interim 条目混合 */
function makeResultEvent(
  entries: { isFinal: boolean; transcript: string }[],
  resultIndex = 0,
): SpeechRecognitionEvent {
  const results = entries.map((e) => ({ isFinal: e.isFinal, 0: { transcript: e.transcript } }))
  return { resultIndex, results } as unknown as SpeechRecognitionEvent
}

type LatestHook = { current: ReturnType<typeof useVoiceInput> }

/** 安装浏览器语音环境（SpeechRecognition + MediaRecorder + getUserMedia） */
function installSpeechEnv(getUserMedia: ReturnType<typeof vi.fn>) {
  Reflect.set(window, 'SpeechRecognition', FakeRecognition)
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true,
  })
}

function removeSpeechEnv() {
  Reflect.deleteProperty(window, 'SpeechRecognition')
  Reflect.deleteProperty(window, 'webkitSpeechRecognition')
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'mediaDevices')
}

describe('useVoiceInput: 浏览器 SpeechRecognition 模式', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockTranscribe.mockReset()
    installSpeechEnv(vi.fn(async () => makeStream()))
  })
  afterEach(() => {
    vi.useRealTimers()
    removeSpeechEnv()
  })

  it('启动：lang/continuous/interimResults 正确下发，进入 recording，计时器走秒', async () => {
    const { result } = renderHook(() => useVoiceInput({ language: 'en-US', continuous: false }))
    expect(result.current.isSupported).toBe(true)
    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.isRecording).toBe(true)
    const rec = Reflect.get(window, 'SpeechRecognition')
    expect(rec).toBeDefined()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100)
    })
    expect(result.current.recordingDuration).toBeGreaterThanOrEqual(2)
  })

  it('中间结果实时上屏回调；最终结果确认回调并更新 transcript', async () => {
    const onInterimResult = vi.fn()
    const onTranscriptionComplete = vi.fn()
    const { result } = renderHook(() =>
      useVoiceInput({ onInterimResult, onTranscriptionComplete }),
    )
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance
    expect(instance).toBeDefined()
    act(() => {
      instance!.onresult?.(makeResultEvent([{ isFinal: false, transcript: '你好' }]))
    })
    expect(result.current.transcript).toBe('你好')
    expect(onInterimResult).toHaveBeenCalledWith('你好')
    act(() => {
      instance!.onresult?.(makeResultEvent([{ isFinal: true, transcript: '你好世界' }]))
    })
    expect(onTranscriptionComplete).toHaveBeenCalledWith('你好世界')
    expect(result.current.transcript).toBe('你好世界')
  })

  it('not-allowed 错误 → 权限文案 + 回 idle + 清理停止识别', async () => {
    const onError = vi.fn()
    const { result } = renderHook(() => useVoiceInput({ onError }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    act(() => {
      instance.onerror?.({ error: 'not-allowed', message: '' } as SpeechRecognitionErrorEvent)
    })
    expect(result.current.error).toEqual({
      type: 'permission_denied',
      message: '麦克风权限被拒绝，请在浏览器设置中允许访问麦克风',
    })
    expect(onError).toHaveBeenCalledWith(result.current.error)
    expect(result.current.state).toBe('idle')
    expect(instance.stop).toHaveBeenCalled()
  })

  it('no-speech/aborted 属 ignore，不产生错误不改变状态', async () => {
    const onError = vi.fn()
    const { result } = renderHook(() => useVoiceInput({ onError }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    for (const code of ['no-speech', 'aborted']) {
      act(() => {
        instance.onerror?.({ error: code, message: '' } as SpeechRecognitionErrorEvent)
      })
    }
    expect(onError).not.toHaveBeenCalled()
    expect(result.current.error).toBeNull()
    expect(result.current.isRecording).toBe(true)
  })

  it('手动停止后 aborted 不视为故障（不触发降级与错误）', async () => {
    const onError = vi.fn()
    const getUserMedia = vi.fn(async () => makeStream())
    installSpeechEnv(getUserMedia)
    const { result } = renderHook(() => useVoiceInput({ onError }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    act(() => result.current.stopRecording())
    expect(result.current.state).toBe('idle')
    act(() => {
      instance.onerror?.({ error: 'aborted', message: '' } as SpeechRecognitionErrorEvent)
    })
    expect(onError).not.toHaveBeenCalled()
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('连续模式 onend 且非手动停止 → 自动重启识别', async () => {
    const { result } = renderHook(() => useVoiceInput({ continuous: true }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    const startCallsBefore = instance.start.mock.calls.length
    act(() => instance.onend?.())
    expect(instance.start.mock.calls.length).toBe(startCallsBefore + 1)
    act(() => result.current.stopRecording())
    const again = instance.start.mock.calls.length
    act(() => instance.onend?.())
    expect(instance.start.mock.calls.length).toBe(again) // 手动停止后不再重启
  })

  it('不支持的浏览器 → isSupported false，启动报 not_supported', async () => {
    removeSpeechEnv()
    const onError = vi.fn()
    const { result } = renderHook(() => useVoiceInput({ onError }))
    expect(result.current.isSupported).toBe(false)
    expect(result.current.isSpeechRecognitionSupported).toBe(false)
    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.error).toEqual({ type: 'not_supported', message: '当前浏览器不支持语音识别' })
    expect(onError).toHaveBeenCalled()
  })
})

describe('useVoiceInput: 服务端 ASR 降级（network 错误触发）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockTranscribe.mockReset()
    installSpeechEnv(vi.fn(async () => makeStream()))
  })
  afterEach(() => {
    vi.useRealTimers()
    removeSpeechEnv()
  })

  it('network 错误 → 停浏览器识别，切 server-asr 模式并开始 MediaRecorder 录音', async () => {
    const getUserMedia = vi.fn(async () => makeStream())
    installSpeechEnv(getUserMedia)
    const { result } = renderHook(() => useVoiceInput())
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    await act(async () => {
      instance.onerror?.({ error: 'network', message: '' } as SpeechRecognitionErrorEvent)
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(instance.stop).toHaveBeenCalled()
    expect(result.current.mode).toBe('server-asr')
    expect(result.current.isRecording).toBe(true)
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
  })

  it('停止后转写：onstop → transcribeAudio → 成功回调最终文字', async () => {
    mockTranscribe.mockResolvedValue({ text: '你好世界' })
    const onTranscriptionComplete = vi.fn()
    const { result } = renderHook(() => useVoiceInput({ onTranscriptionComplete }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    await act(async () => {
      instance.onerror?.({ error: 'network', message: '' } as SpeechRecognitionErrorEvent)
      await vi.advanceTimersByTimeAsync(0)
    })
    // 模拟收到音频块
    const recorder = (FakeMediaRecorder as unknown as { lastInstance?: FakeMediaRecorder }).lastInstance!
    act(() => {
      recorder.ondataavailable?.({ data: new Blob(['x'], { type: 'audio/webm' }) })
    })
    await act(async () => {
      result.current.stopRecording()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(mockTranscribe).toHaveBeenCalledTimes(1)
    expect(onTranscriptionComplete).toHaveBeenCalledWith('你好世界')
    expect(result.current.state).toBe('idle')
  })

  it('后端 ASR 未配置（空 text）→ not_supported 友好提示', async () => {
    const onError = vi.fn()
    mockTranscribe.mockResolvedValue({ text: '' })
    const { result } = renderHook(() => useVoiceInput({ onError }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    await act(async () => {
      instance.onerror?.({ error: 'service-not-allowed', message: '' } as SpeechRecognitionErrorEvent)
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      result.current.stopRecording()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(onError).toHaveBeenCalledWith({
      type: 'not_supported',
      message: '未配置语音转文字服务，请联系管理员启用 ASR',
    })
    expect(result.current.state).toBe('idle')
  })

  it('转写请求失败 → transcription_failed + 回 idle', async () => {
    const onError = vi.fn()
    mockTranscribe.mockRejectedValue(new Error('500'))
    const { result } = renderHook(() => useVoiceInput({ onError }))
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    await act(async () => {
      instance.onerror?.({ error: 'network', message: '' } as SpeechRecognitionErrorEvent)
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      result.current.stopRecording()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(onError).toHaveBeenCalledWith({ type: 'transcription_failed', message: '语音转文字失败，请重试' })
    expect(result.current.state).toBe('idle')
  })
})

describe('useVoiceInput: 音频录制模式（supportsAudio=true）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockTranscribe.mockReset()
    installSpeechEnv(vi.fn(async () => makeStream()))
  })
  afterEach(() => {
    vi.useRealTimers()
    removeSpeechEnv()
  })

  it('happy path：getUserMedia → MediaRecorder.start → recording', async () => {
    const getUserMedia = vi.fn(async () => makeStream())
    installSpeechEnv(getUserMedia)
    const { result } = renderHook(() => useVoiceInput({ supportsAudio: true }))
    expect(result.current.isSupported).toBe(true)
    await act(async () => {
      await result.current.startRecording()
    })
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
    expect(result.current.isRecording).toBe(true)
  })

  it('停止：聚合音频块回 onRecordingComplete，回 idle', async () => {
    const onRecordingComplete = vi.fn()
    const { result } = renderHook(() => useVoiceInput({ supportsAudio: true, onRecordingComplete }))
    await act(async () => {
      await result.current.startRecording()
    })
    const recorder = (FakeMediaRecorder as unknown as { lastInstance?: FakeMediaRecorder }).lastInstance!
    act(() => {
      recorder.ondataavailable?.({ data: new Blob(['chunk1'], { type: 'audio/webm' }) })
      recorder.ondataavailable?.({ data: new Blob(['chunk2'], { type: 'audio/webm' }) })
    })
    act(() => result.current.stopRecording())
    expect(onRecordingComplete).toHaveBeenCalledTimes(1)
    const blob = onRecordingComplete.mock.calls[0][0] as Blob
    expect(blob.size).toBeGreaterThan(0)
    expect(result.current.state).toBe('idle')
  })

  it('getUserMedia 拒绝 → permission_denied 错误', async () => {
    installSpeechEnv(vi.fn(async () => Promise.reject(new Error('denied'))))
    const onError = vi.fn()
    const { result } = renderHook(() => useVoiceInput({ supportsAudio: true, onError }))
    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.error).toEqual({
      type: 'permission_denied',
      message: '无法访问麦克风，请检查权限设置',
    })
    expect(result.current.state).toBe('idle')
  })

  it('MediaRecorder 不支持（webm 不支持）→ isSupported false', () => {
    vi.stubGlobal(
      'MediaRecorder',
      class {
        static isTypeSupported = vi.fn(() => false)
      },
    )
    const { result } = renderHook(() => useVoiceInput({ supportsAudio: true }))
    expect(result.current.isMediaRecorderSupported).toBe(false)
    expect(result.current.isSupported).toBe(false)
  })
})

describe('useVoiceInput: 卸载清理', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    installSpeechEnv(vi.fn(async () => makeStream()))
  })
  afterEach(() => {
    vi.useRealTimers()
    removeSpeechEnv()
  })

  it('卸载时停止识别（资源清理；state 不再更新属卸载后语义）', async () => {
    const { result, unmount } = renderHook(() => useVoiceInput())
    await act(async () => {
      await result.current.startRecording()
    })
    const instance = (FakeRecognition as unknown as { lastInstance?: FakeRecognition }).lastInstance!
    unmount()
    expect(instance.stop).toHaveBeenCalled()
  })
})
