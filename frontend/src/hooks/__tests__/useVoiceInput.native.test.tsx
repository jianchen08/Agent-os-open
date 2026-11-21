// @feature FP-0.2.shell_speech 移动端原生 ASR 桥（ShellSpeech native 通道契约测试）
/** @ci frontend-test */
/**
 * 覆盖：探测可用 → native 通道全链（start 下发语言 / partial 实时上屏 /
 * final 提交 / stop 后落 final 回 idle）、探测不可用回退 server-asr、
 * start 拒绝回退 server-asr、运行时错误未产出文本回退、已产出文本报错不回退、
 * cancel 丢弃在途事件、preferBrowser 保持浏览器路径不触碰桥。
 *
 * 隔离策略：ShellSpeech 桥以最小替身注入 window.Capacitor.Plugins（壳 WebView
 * 注入形态）；server-asr 回退链路复用 MediaRecorder/getUserMedia/transcribeAudio
 * 替身（与 useVoiceInput.test.tsx 既有隔离策略一致）。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useVoiceInput } from '../useVoiceInput'
import type {
  SpeechRecognitionEvent,
  UseVoiceInputOptions,
} from '@/types/voiceInput'

const mockTranscribe = vi.fn()
/**
 * TranscriptionError 替身：与 services/api/asr 的导出同类（instanceof 判定用）。
 * vi.mock 工厂在导入期解引用该类，须经 vi.hoisted 提升到 mock 声明之前。
 */
const { FakeTranscriptionError } = vi.hoisted(() => {
  class FakeTranscriptionError extends Error {
    code?: string
    constructor(message: string, code?: string) {
      super(message)
      this.name = 'TranscriptionError'
      this.code = code
    }
  }
  return { FakeTranscriptionError }
})
vi.mock('@/services/api/asr', () => ({
  transcribeAudio: (...args: unknown[]) => mockTranscribe(...args),
  TranscriptionError: FakeTranscriptionError,
}))

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

/** 浏览器 SpeechRecognition 替身：仅 preferBrowser 回归用例用（记录启动事实） */
class FakeRecognition {
  static lastInstance?: FakeRecognition
  lang = ''
  continuous = false
  interimResults = false
  start = vi.fn()
  stop = vi.fn()
  onresult: ((e: SpeechRecognitionEvent) => void) | null = null
  onend: (() => void) | null = null

  constructor() {
    FakeRecognition.lastInstance = this
  }
}

function lastRecorder() {
  return (FakeMediaRecorder as unknown as { lastInstance?: FakeMediaRecorder })
    .lastInstance!
}

function makeStream(): MediaStream {
  return { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream
}

/** ShellSpeech 桥替身：记录方法调用与监听器，事件可手动 emit */
type ShellSpeechEventData = { transcript?: string; code?: string }
type ShellSpeechListener = (data: ShellSpeechEventData) => void

class FakeShellSpeechBridge {
  available = vi.fn(async () => ({ available: true }))
  start = vi.fn(async () => {})
  stop = vi.fn(async () => {})
  cancel = vi.fn(async () => {})
  private listeners = new Map<string, ShellSpeechListener[]>()
  addListener = vi.fn(
    (event: string, cb: ShellSpeechListener) => {
      const list = this.listeners.get(event) ?? []
      list.push(cb)
      this.listeners.set(event, list)
      return Promise.resolve({
        remove: () => {
          this.listeners.set(
            event,
            (this.listeners.get(event) ?? []).filter((l) => l !== cb),
          )
          return Promise.resolve()
        },
      })
    },
  )

  emit(event: string, data: ShellSpeechEventData) {
    ;(this.listeners.get(event) ?? []).forEach((cb) => cb(data))
  }

  listenerCount(event: string): number {
    return (this.listeners.get(event) ?? []).length
  }
}

/** 安装壳环境（Capacitor.Plugins.ShellSpeech 桥）+ 录音环境（无浏览器识别） */
function installNativeEnv(bridge: FakeShellSpeechBridge) {
  Reflect.set(window, 'Capacitor', { Plugins: { ShellSpeech: bridge } })
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(async () => makeStream()) },
    configurable: true,
  })
}

function removeNativeEnv() {
  Reflect.deleteProperty(window, 'Capacitor')
  Reflect.deleteProperty(window, 'SpeechRecognition')
  Reflect.deleteProperty(window, 'webkitSpeechRecognition')
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'mediaDevices')
}

beforeEach(() => {
  vi.useFakeTimers()
  mockTranscribe.mockReset()
  FakeMediaRecorder.lastInstance = undefined
  FakeRecognition.lastInstance = undefined
})

afterEach(() => {
  vi.useRealTimers()
  removeNativeEnv()
})

/** 挂载 hook 并走 startRecording（native 环境已装好），推进 timers 使装配链落定 */
async function startRecordingWithBridge(
  bridge: FakeShellSpeechBridge,
  options: UseVoiceInputOptions = {},
) {
  const { result, unmount } = renderHook(() => useVoiceInput({ language: 'zh-CN', ...options }))
  await act(async () => {
    await result.current.startRecording()
    await vi.advanceTimersByTimeAsync(0)
  })
  return { result, unmount }
}

describe('useVoiceInput native 通道：探测可用 → 全链', () => {
  it('startRecording 走 native：available 探测、start 下发语言、进入 recording、mode=native', async () => {
    const bridge = new FakeShellSpeechBridge()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge)

    expect(bridge.available).toHaveBeenCalledTimes(1)
    expect(bridge.start).toHaveBeenCalledTimes(1)
    expect(bridge.start).toHaveBeenCalledWith({ language: 'zh-CN' })
    expect(result.current.state).toBe('recording')
    expect(result.current.mode).toBe('native')
    unmount()
  })

  it('partial 实时上屏（onInterimResult + transcript），final 提交（onTranscriptionComplete）', async () => {
    const bridge = new FakeShellSpeechBridge()
    const onInterim = vi.fn()
    const onFinal = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, {
      onInterimResult: onInterim,
      onTranscriptionComplete: onFinal,
    })

    await act(async () => {
      bridge.emit('speechPartial', { transcript: '你好' })
    })
    expect(result.current.transcript).toBe('你好')
    expect(onInterim).toHaveBeenCalledWith('你好')

    await act(async () => {
      bridge.emit('speechFinal', { transcript: '你好世界' })
    })
    expect(onFinal).toHaveBeenCalledTimes(1)
    expect(onFinal).toHaveBeenCalledWith('你好世界')
    // 持续识别：final 段落落定后录音态保持（Java 侧续听下一段）
    expect(result.current.state).toBe('recording')
    unmount()
  })

  it('停止：bridge.stop 下发 → transcribing → final 落地后提交并回 idle', async () => {
    const bridge = new FakeShellSpeechBridge()
    const onFinal = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, {
      onTranscriptionComplete: onFinal,
    })

    await act(async () => {
      result.current.stopRecording()
    })
    expect(bridge.stop).toHaveBeenCalledTimes(1)
    expect(result.current.state).toBe('transcribing')

    await act(async () => {
      bridge.emit('speechFinal', { transcript: '松手后最终文本' })
    })
    expect(onFinal).toHaveBeenCalledWith('松手后最终文本')
    expect(result.current.state).toBe('idle')
    unmount()
  })

  it('取消：bridge.cancel 下发，在途 final 事件被丢弃不提交', async () => {
    const bridge = new FakeShellSpeechBridge()
    const onFinal = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, {
      onTranscriptionComplete: onFinal,
    })

    await act(async () => {
      result.current.cancelRecording()
    })
    expect(bridge.cancel).toHaveBeenCalledTimes(1)
    expect(result.current.state).toBe('idle')

    await act(async () => {
      bridge.emit('speechFinal', { transcript: '取消后迟到文本' })
    })
    expect(onFinal).not.toHaveBeenCalled()
    unmount()
  })
})

describe('useVoiceInput native 通道：回退 server-asr', () => {
  it('探测不可用（available=false）→ 回退 server-asr 录音转写链路', async () => {
    const bridge = new FakeShellSpeechBridge()
    bridge.available.mockResolvedValue({ available: false })
    mockTranscribe.mockResolvedValue({ text: '服务端转写' })
    const onFinal = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, {
      onTranscriptionComplete: onFinal,
    })

    // 未触碰 native 启动，通道切到 server-asr
    expect(bridge.start).not.toHaveBeenCalled()
    expect(result.current.mode).toBe('server-asr')
    expect(result.current.state).toBe('recording')
    expect(lastRecorder()).toBeDefined()

    // 回退链路完整可用：停止 → 转写 → 提交
    const recorder = lastRecorder()
    act(() => {
      recorder.ondataavailable?.({ data: new Blob(['x'], { type: 'audio/webm' }) })
    })
    await act(async () => {
      result.current.stopRecording()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(mockTranscribe).toHaveBeenCalledTimes(1)
    expect(onFinal).toHaveBeenCalledWith('服务端转写')
    expect(result.current.state).toBe('idle')
    unmount()
  })

  it('available() 抛错（桥异常）→ 视同不可用，回退 server-asr', async () => {
    const bridge = new FakeShellSpeechBridge()
    bridge.available.mockRejectedValue(new Error('bridge gone'))
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge)

    expect(bridge.start).not.toHaveBeenCalled()
    expect(result.current.mode).toBe('server-asr')
    expect(result.current.state).toBe('recording')
    unmount()
  })

  it('start() 拒绝（启动报错）→ 回退 server-asr', async () => {
    const bridge = new FakeShellSpeechBridge()
    bridge.start.mockRejectedValue(new Error('speech service unavailable'))
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge)

    expect(result.current.mode).toBe('server-asr')
    expect(result.current.state).toBe('recording')
    expect(lastRecorder()).toBeDefined()
    unmount()
  })

  it('运行时错误未产出文本（network）→ 静默回退 server-asr，不向用户报错', async () => {
    const bridge = new FakeShellSpeechBridge()
    const onError = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, { onError })

    await act(async () => {
      bridge.emit('speechError', { code: 'network' })
    })
    expect(onError).not.toHaveBeenCalled()
    expect(result.current.mode).toBe('server-asr')
    expect(result.current.state).toBe('recording')
    expect(lastRecorder()).toBeDefined()
    unmount()
  })

  it('运行时错误已产出文本（network）→ 报错终止，不重启录音', async () => {
    const bridge = new FakeShellSpeechBridge()
    const onError = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, { onError })

    await act(async () => {
      bridge.emit('speechPartial', { transcript: '已识别前半' })
    })
    await act(async () => {
      bridge.emit('speechError', { code: 'network' })
    })
    expect(onError).toHaveBeenCalledTimes(1)
    const error = onError.mock.calls[0][0] as { type: string; message: string }
    expect(error.type).toBe('transcription_failed')
    expect(error.message.length).toBeGreaterThan(0)
    expect(result.current.state).toBe('idle')
    // 不回退：录音链路未启动
    expect(lastRecorder()).toBeUndefined()
    unmount()
  })

  it('忽略类错误（no-speech）→ 静默，通道保持 native 录音态', async () => {
    const bridge = new FakeShellSpeechBridge()
    const onError = vi.fn()
    installNativeEnv(bridge)
    const { result, unmount } = await startRecordingWithBridge(bridge, { onError })

    await act(async () => {
      bridge.emit('speechError', { code: 'no-speech' })
    })
    expect(onError).not.toHaveBeenCalled()
    expect(result.current.state).toBe('recording')
    expect(result.current.mode).toBe('native')
    unmount()
  })
})

describe('useVoiceInput native 通道：纯浏览器路径零改动回归', () => {
  it('preferBrowser=true → 浏览器 SpeechRecognition，桥不被触碰', async () => {
    const bridge = new FakeShellSpeechBridge()
    installNativeEnv(bridge)
    Reflect.set(window, 'SpeechRecognition', FakeRecognition)
    const { result, unmount } = await startRecordingWithBridge(bridge, {
      preferBrowser: true,
    })

    expect(bridge.available).not.toHaveBeenCalled()
    expect(bridge.start).not.toHaveBeenCalled()
    expect(FakeRecognition.lastInstance).not.toBeUndefined()
    expect(result.current.mode).toBe('browser')
    unmount()
  })

  it('无壳环境（window.Capacitor 不存在）→ 桌面现状不变：默认 server-asr', async () => {
    const getUserMedia = vi.fn(async () => makeStream())
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia },
      configurable: true,
    })
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
    mockTranscribe.mockResolvedValue({ text: '桌面直录' })
    const { result, unmount } = renderHook(() => useVoiceInput())
    await act(async () => {
      await result.current.startRecording()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.mode).toBe('server-asr')
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
    unmount()
  })
})
