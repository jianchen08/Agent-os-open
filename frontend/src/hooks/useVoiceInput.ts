/**
 * 语音输入 Hook
 *
 * 录音与识别通道：
 * - native：壳原生 SpeechRecognizer 实时识别（Android 壳内原生优先；
 *   探测不可用/启动失败/未产出文本的运行时错误自动回退 server-asr）
 * - browser：浏览器原生 SpeechRecognition 实时识别（桌面按钮路径实时上屏）
 * - server-asr：MediaRecorder 录音 → 停止后转 WAV → 整段服务端转写（显式
 *   startServerASR() 与 native/browser 识别不可用/报错降级共用）
 * - supportsAudio：模型支持音频输入时录制音频 Blob，经 onRecordingComplete 落附件，不做转写
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { startNativeSpeech } from '@/hooks/nativeSpeech'
import { TranscriptionError, transcribeAudio } from '@/services/api/asr'
import { getShellSpeech } from '@/services/shellPlugins'
import { convertBlobToWav } from '@/utils/audioWav'
import type { NativeSpeechSession } from '@/hooks/nativeSpeech'
import type {
  SpeechRecognitionConstructor,
  SpeechRecognitionErrorEvent,
  SpeechRecognitionEvent,
  SpeechRecognitionInstance,
  UseVoiceInputOptions,
  UseVoiceInputReturn,
  VoiceInputError,
  VoiceInputState,
} from '@/types/voiceInput'
import type { Dispatch, SetStateAction } from 'react'

/** 默认录音时长上限（秒）：到点自动停止并按当前通道走后续流程 */
const DEFAULT_MAX_DURATION_SECONDS = 60

/**
 * 获取 SpeechRecognition 构造函数
 *
 * 处理浏览器前缀兼容性
 */
function getSpeechRecognition(): SpeechRecognitionConstructor | null {
  if (typeof window === 'undefined') return null

  return window.SpeechRecognition || window.webkitSpeechRecognition || null
}

/** 识别错误的处置决策：文案/类型 + 是否需要切服务端 ASR 降级 */
interface RecognitionErrorDecision {
  errorMessage: string
  errorType: VoiceInputError['type']
  shouldFallbackToServer: boolean
}

/** 手动停止场景下可吞掉的停止相关错误（不视为故障） */
const STOP_RELATED_ERRORS = ['aborted', 'no-speech', 'network', 'service-not-allowed']

/** 纯函数：SpeechRecognition 错误码 → 用户文案 + 错误类型 + 降级决策（映射表） */
function decideRecognitionError(error: string): RecognitionErrorDecision | 'ignore' {
  switch (error) {
    case 'not-allowed':
    case 'permission-denied':
      return {
        errorMessage: '麦克风权限被拒绝，请在浏览器设置中允许访问麦克风',
        errorType: 'permission_denied',
        shouldFallbackToServer: false,
      }
    case 'no-speech':
      return 'ignore'
    case 'audio-capture':
      return {
        errorMessage: '无法捕获音频，请检查麦克风设备',
        errorType: 'transcription_failed',
        shouldFallbackToServer: false,
      }
    case 'network':
      // 浏览器云端语音服务不可达，自动降级到服务端 ASR
      return { errorMessage: '', errorType: 'transcription_failed', shouldFallbackToServer: true }
    case 'aborted':
      return 'ignore'
    case 'service-not-allowed':
      // 语音识别服务不可用，自动降级到服务端 ASR
      return { errorMessage: '', errorType: 'transcription_failed', shouldFallbackToServer: true }
    default:
      return {
        errorMessage: '语音识别失败',
        errorType: 'transcription_failed',
        shouldFallbackToServer: false,
      }
  }
}

/**
 * 启动录音计时器（每秒 +1；重复启动时先清旧定时器）。
 * 达到 maxSeconds 时停表并回调 onLimit（调用方按当前通道走正常停止流程）。
 */
function startDurationTimer(
  timerRef: { current: ReturnType<typeof setInterval> | null },
  durationRef: { current: number },
  setRecordingDuration: Dispatch<SetStateAction<number>>,
  maxSeconds: number,
  onLimit: () => void,
): void {
  if (timerRef.current) {
    clearInterval(timerRef.current)
  }
  durationRef.current = 0
  setRecordingDuration(0)
  timerRef.current = setInterval(() => {
    durationRef.current += 1
    setRecordingDuration(durationRef.current)
    if (durationRef.current >= maxSeconds) {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
      onLimit()
    }
  }, 1000)
}

/**
 * 语音输入 Hook
 *
 * @param options - 配置选项
 * @returns 语音输入控制接口
 */
export function useVoiceInput(options: UseVoiceInputOptions = {}): UseVoiceInputReturn {
  const {
    supportsAudio = false,
    language = 'zh-CN',
    continuous = true,
    preferBrowser = false,
    maxDurationSeconds = DEFAULT_MAX_DURATION_SECONDS,
    onRecordingComplete,
    onTranscriptionComplete,
    onInterimResult,
    onError,
  } = options

  const [state, setState] = useState<VoiceInputState>('idle')
  const [transcript, setTranscript] = useState('')
  const [error, setError] = useState<VoiceInputError | null>(null)
  const [recordingDuration, setRecordingDuration] = useState(0)
  /**
   * 当前识别模式：
   * - native：壳原生 SpeechRecognizer（Android 壳内原生优先，实时）
   * - browser：浏览器原生 SpeechRecognition（实时）
   * - server-asr：服务端 ASR（整段转写；显式入口与 native/browser 不可用/报错时复用）
   */
  const [mode, setMode] = useState<'native' | 'browser' | 'server-asr'>('browser')

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const isRecordingRef = useRef(false)
  const isManualStopRef = useRef(false)
  const durationTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const durationRef = useRef(0)
  const modeRef = useRef<'native' | 'browser' | 'server-asr'>('browser')
  /** 活跃的 native 会话（壳 ShellSpeech 桥装配产物；stop/cancel/cleanup 消费） */
  const nativeSessionRef = useRef<NativeSpeechSession | null>(null)
  /** 录音会话代号：停止/取消/新起录都会推进，用于作废在途 getUserMedia 与已停止会话 */
  const sessionGenRef = useRef(0)
  /** 当前生效的录音会话（onstop 归属判定：新会话起录后旧会话的迟到回调不再上抛） */
  const activeRecorderSessionRef = useRef(0)
  /** 本次录音是否被取消：onstop 据此丢弃音频，不转写、不落附件 */
  const cancelledRef = useRef(false)
  /** 自动停表上限回调需要最新 stopRecording（计时器启动早于回调声明） */
  const stopRecordingRef = useRef<() => void>(() => {})

  const isSpeechRecognitionSupported = Boolean(getSpeechRecognition())
  const isMediaRecorderSupported =
    typeof window !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    typeof MediaRecorder !== 'undefined' &&
    MediaRecorder.isTypeSupported('audio/webm')
  /** 壳原生识别桥可用（仅壳 WebView 存在；浏览器/桌面/旧包壳均 null） */
  const shellSpeechAvailable = Boolean(getShellSpeech())

  // 可录音但无浏览器识别（如 Android WebView）同样有语音入口：原生桥优先，否则服务端 ASR
  const isSupported = supportsAudio
    ? isMediaRecorderSupported
    : isSpeechRecognitionSupported || isMediaRecorderSupported || shellSpeechAvailable

  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  /**
   * 清理所有资源
   */
  const cleanup = useCallback(() => {
    isRecordingRef.current = false
    isManualStopRef.current = true

    if (durationTimerRef.current) {
      clearInterval(durationTimerRef.current)
      durationTimerRef.current = null
    }

    if (nativeSessionRef.current) {
      nativeSessionRef.current.cancel()
      nativeSessionRef.current = null
    }

    if (recognitionRef.current) {
      try {
        recognitionRef.current.stop()
      } catch {
        // 忽略停止时的错误
      }
      recognitionRef.current = null
    }

    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      try {
        mediaRecorderRef.current.stop()
      } catch {
        // 忽略停止时的错误
      }
      mediaRecorderRef.current = null
    }

    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop())
      streamRef.current = null
    }

    audioChunksRef.current = []
  }, [])

  /**
   * 处理错误
   */
  const handleError = useCallback(
    (type: VoiceInputError['type'], message: string) => {
      const errorInfo: VoiceInputError = { type, message }
      setError(errorInfo)
      setState('idle')
      cleanup()
      onError?.(errorInfo)
    },
    [cleanup, onError],
  )

  /**
   * 启动 MediaRecorder 录音会话
   *
   * handleStop 只在该会话正常停止（未被取消、未被新会话取代）时收到聚合音频 Blob。
   * 会话代号守卫覆盖两句竞态：①getUserMedia 在途时用户已松手/取消——不进入录音态；
   * ②正常停止后立刻再次起录——旧会话的迟到 onstop 不回调。
   */
  const beginRecorder = useCallback(
    async (handleStop: (blob: Blob) => void) => {
      const session = ++sessionGenRef.current
      cancelledRef.current = false
      // 起录即时反馈：横幅先亮，不等 getUserMedia（权限弹窗/设备枚举可能耗时）
      setState('recording')

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        if (session !== sessionGenRef.current) {
          // 起录在途已停止/取消：释放设备，不进入录音态
          stream.getTracks().forEach((track) => track.stop())
          return
        }
        streamRef.current = stream

        const mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })

        mediaRecorder.ondataavailable = (event) => {
          if (event.data.size > 0) {
            audioChunksRef.current.push(event.data)
          }
        }

        mediaRecorder.onstop = () => {
          const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
          audioChunksRef.current = []
          if (cancelledRef.current || activeRecorderSessionRef.current !== session) return
          handleStop(audioBlob)
        }

        mediaRecorderRef.current = mediaRecorder
        activeRecorderSessionRef.current = session
        mediaRecorder.start()
        isRecordingRef.current = true
        startDurationTimer(
          durationTimerRef,
          durationRef,
          setRecordingDuration,
          maxDurationSeconds,
          () => stopRecordingRef.current(),
        )
      } catch {
        if (session !== sessionGenRef.current) return
        handleError('permission_denied', '无法访问麦克风，请检查权限设置')
      }
    },
    [handleError, maxDurationSeconds],
  )

  /** 服务端 ASR：停止后整段转写；取消（会话代号推进）则丢弃结果 */
  const handleServerAsrStop = useCallback(
    async (audioBlob: Blob) => {
      setState('transcribing')
      const session = sessionGenRef.current
      try {
        // 上传前转 WAV：智谱 ASR 不收 webm 容器（code=1214），MediaRecorder 只产 webm。
        // 解码不可用/失败时回退原格式，由服务端裁决（不阻断上传链路）。
        let uploadBlob = audioBlob
        let uploadMime = 'audio/webm'
        try {
          uploadBlob = await convertBlobToWav(audioBlob)
          uploadMime = 'audio/wav'
        } catch {
          // 回退原格式
        }
        const result = await transcribeAudio(uploadBlob, uploadMime)
        if (session !== sessionGenRef.current) return
        if (result?.text) {
          onTranscriptionComplete?.(result.text)
        } else {
          // 后端 ASR 未配置（503），指路设置页配置入口
          onError?.({
            type: 'not_supported',
            message: '未配置语音转文字服务，请在 设置 → 插件配置 → 语音转写（ASR）中配置',
          })
        }
      } catch (err) {
        if (session !== sessionGenRef.current) return
        // 结构化失败（502 等）横幅补后端原因；其余错误保持通用文案
        onError?.({
          type: 'transcription_failed',
          message:
            err instanceof TranscriptionError
              ? `语音转文字失败：${err.message}`
              : '语音转文字失败，请重试',
        })
      } finally {
        if (session === sessionGenRef.current) setState('idle')
      }
    },
    [onTranscriptionComplete, onError],
  )

  /** 音频模型：录音完成交附件管线，不转写 */
  const handleRecordingStop = useCallback(
    (audioBlob: Blob) => {
      onRecordingComplete?.(audioBlob)
      setState('idle')
    },
    [onRecordingComplete],
  )

  /**
   * 启动服务端 ASR 录音（显式主通道）
   *
   * 录音停止时上传音频到后端，转写结果通过 onTranscriptionComplete 回调返回。
   * 浏览器识别不可用（Android WebView）或报 network/service-not-allowed 时都走此通道。
   */
  const startServerASR = useCallback(async () => {
    if (!isMediaRecorderSupported) {
      handleError('not_supported', '当前设备不支持录音')
      return
    }
    setError(null)
    setTranscript('')
    setMode('server-asr')
    isManualStopRef.current = false
    await beginRecorder(handleServerAsrStop)
  }, [isMediaRecorderSupported, handleError, beginRecorder, handleServerAsrStop])

  /**
   * 原生识别通道（壳 ShellSpeech 桥 → Android 系统 SpeechRecognizer）
   *
   * 探测不可用 / start 失败 / 未产出文本的运行时错误 → 回退 server-asr；
   * 已产出文本后的失败报错终止（回退会重启录音丢已识别文本）。
   * 会话代号守卫：装配在途被停止/取消时丢弃会话，不进入录音态。
   */
  const startNativeRecognition = useCallback(async () => {
    const bridge = getShellSpeech()
    if (!bridge) {
      await startServerASR()
      return
    }
    setError(null)
    setTranscript('')
    setMode('native')
    const gen = ++sessionGenRef.current
    const alive = () => sessionGenRef.current === gen
    const session = await startNativeSpeech(bridge, language, {
      onInterim: (text) => {
        if (!alive()) return
        setTranscript(text)
        onInterimResult?.(text)
      },
      onFinal: (text) => {
        if (!alive()) return
        setTranscript(text)
        onTranscriptionComplete?.(text)
      },
      onFallback: () => {
        nativeSessionRef.current = null
        if (!alive()) return
        void startServerASR()
      },
      onFatal: (errorInfo) => {
        nativeSessionRef.current = null
        if (!alive()) return
        handleError(errorInfo.type, errorInfo.message)
      },
      onSettled: () => {
        nativeSessionRef.current = null
        if (!alive()) return
        setState('idle')
        isRecordingRef.current = false
      },
    }).catch((_assemblyError: unknown) => {
      // 块状处理登记：装配异常与返回 null 同归回退口（下方 if (!session) →
      // startServerASR），按「原生不可用」语义回退而非吞错；回退后链路失败
      // 由 server-asr 自身错误面承载（用户可感知）。
      return null
    })

    if (!session) {
      if (!alive()) return
      await startServerASR()
      return
    }
    if (!alive()) {
      // 装配在途已停止/取消：丢弃会话
      session.cancel()
      return
    }
    nativeSessionRef.current = session
    isManualStopRef.current = false
    setState('recording')
    isRecordingRef.current = true
    startDurationTimer(
      durationTimerRef,
      durationRef,
      setRecordingDuration,
      maxDurationSeconds,
      () => stopRecordingRef.current(),
    )
  }, [
    language,
    maxDurationSeconds,
    handleError,
    onInterimResult,
    onTranscriptionComplete,
    startServerASR,
  ])

  /**
   * 初始化语音识别
   */
  const initSpeechRecognition = useCallback(() => {
    const SpeechRecognitionClass = getSpeechRecognition()
    if (!SpeechRecognitionClass) {
      handleError('not_supported', '当前浏览器不支持语音识别')
      return null
    }

    const recognition = new SpeechRecognitionClass()
    recognition.lang = language
    recognition.continuous = continuous
    recognition.interimResults = true

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      let finalTranscript = ''
      let interimTranscript = ''

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i]
        if (result.isFinal) {
          finalTranscript += result[0].transcript
        } else {
          interimTranscript += result[0].transcript
        }
      }

      // 临时结果实时回调（驱动输入框实时显示候选文字）
      if (interimTranscript) {
        setTranscript(interimTranscript)
        onInterimResult?.(interimTranscript)
      }

      // 最终确认结果回调（由调用方追加到已确认文字）
      if (finalTranscript) {
        setTranscript(finalTranscript)
        onTranscriptionComplete?.(finalTranscript)
      }
    }

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      if (isManualStopRef.current && STOP_RELATED_ERRORS.includes(event.error)) {
        return
      }

      const decision = decideRecognitionError(event.error)
      if (decision === 'ignore') {
        return
      }

      if (decision.shouldFallbackToServer) {
        // 停止浏览器识别，切换到服务端 ASR 降级模式
        if (recognitionRef.current) {
          try {
            recognitionRef.current.stop()
          } catch {
            // 忽略停止错误
          }
        }
        startServerASR()
        return
      }

      handleError(decision.errorType, decision.errorMessage)
    }

    return recognition
  }, [language, continuous, handleError, onTranscriptionComplete, onInterimResult, startServerASR])

  /** 浏览器 SpeechRecognition 实时识别（preferBrowser 路径与仅剩识别能力时的兜底） */
  const startBrowserRecognition = useCallback(() => {
    const recognition = initSpeechRecognition()
    if (!recognition) return

    recognitionRef.current = recognition
    recognition.start()
    setState('recording')
    isRecordingRef.current = true
    startDurationTimer(
      durationTimerRef,
      durationRef,
      setRecordingDuration,
      maxDurationSeconds,
      () => stopRecordingRef.current(),
    )
  }, [initSpeechRecognition, maxDurationSeconds])

  /**
   * 开始录音
   *
   * 通道选择：音频模型 → 录音落附件；壳内默认 → 原生 SpeechRecognizer
   * （探测不可用/启动失败自动回退服务端 ASR）；桌面默认 → 服务端 ASR 直录
   * （Electron/国内 Chrome 无云端识别服务，先跑浏览器识别会空转丢话）；
   * preferBrowser 或只剩识别能力 → 浏览器实时识别。
   */
  const startRecording = useCallback(async () => {
    if (!isSupported) {
      handleError('not_supported', '当前浏览器不支持语音识别')
      return
    }

    setError(null)
    setTranscript('')
    setRecordingDuration(0)
    setMode('browser')
    cancelledRef.current = false
    isManualStopRef.current = false

    if (supportsAudio) {
      await beginRecorder(handleRecordingStop)
      return
    }

    // 壳内原生优先（preferBrowser 显式选浏览器路径时让位；回退目标 server-asr
    // 需要录音能力，故以 MediaRecorder 可用为前置——无录音能力时保持既有通道序）
    if (!preferBrowser && isMediaRecorderSupported && getShellSpeech()) {
      await startNativeRecognition()
      return
    }

    if (isMediaRecorderSupported && !preferBrowser) {
      await startServerASR()
      return
    }

    if (isSpeechRecognitionSupported) {
      startBrowserRecognition()
      return
    }

    // 有录音能力但无浏览器识别（如 Android WebView）：服务端 ASR 主通道
    await startServerASR()
  }, [
    isSupported,
    supportsAudio,
    preferBrowser,
    isMediaRecorderSupported,
    isSpeechRecognitionSupported,
    handleError,
    beginRecorder,
    handleRecordingStop,
    startServerASR,
    startNativeRecognition,
    startBrowserRecognition,
  ])

  /**
   * 停止录音
   *
   * 正常停止：native 停听等最终结果落地（回 idle）；服务端 ASR 进入转写态
   * （横幅可见），音频模型交附件管线，浏览器识别回空闲。
   */
  const stopRecording = useCallback(() => {
    // native 通道：不推进会话代号（最终结果事件需在途落地），转 transcribing 等收尾
    if (modeRef.current === 'native' && nativeSessionRef.current) {
      const session = nativeSessionRef.current
      isRecordingRef.current = false
      if (durationTimerRef.current) {
        clearInterval(durationTimerRef.current)
        durationTimerRef.current = null
      }
      setState('transcribing')
      session.stop()
      return
    }
    sessionGenRef.current += 1
    const wasServerAsr = modeRef.current === 'server-asr'
    const hadRecorder = mediaRecorderRef.current !== null
    cleanup()
    setState(wasServerAsr && hadRecorder ? 'transcribing' : 'idle')
  }, [cleanup])

  useEffect(() => {
    stopRecordingRef.current = stopRecording
  }, [stopRecording])

  /**
   * 取消录音：停止并丢弃（不转写、不落附件、清空音频块）；转写中取消则丢弃结果。
   */
  const cancelRecording = useCallback(() => {
    sessionGenRef.current += 1
    cancelledRef.current = true
    cleanup()
    setState('idle')
  }, [cleanup])

  useEffect(() => {
    return () => {
      cleanup()
    }
  }, [cleanup])

  // 连续模式下自动重启语音识别
  useEffect(() => {
    if (!continuous || state !== 'recording' || supportsAudio) return

    const recognition = recognitionRef.current
    if (!recognition) return

    const handleEnd = () => {
      if (isRecordingRef.current && !isManualStopRef.current) {
        try {
          recognition.start()
        } catch {
          // 忽略重启时的错误
        }
      }
    }

    recognition.onend = handleEnd

    return () => {
      recognition.onend = null
    }
  }, [continuous, state, supportsAudio])

  return {
    state,
    isRecording: state === 'recording',
    isTranscribing: state === 'transcribing',
    transcript,
    recordingDuration,
    mode,
    error,
    startRecording,
    stopRecording,
    startServerASR,
    cancelRecording,
    isSupported,
    isSpeechRecognitionSupported,
    isMediaRecorderSupported,
  }
}
