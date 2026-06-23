/**
 * 语音输入 Hook
 *
 * 提供语音识别和音频录制功能：
 * - 使用 Web Speech API (SpeechRecognition) 进行实时语音转文字
 * - 使用 MediaRecorder 录制 WebM 音频
 * - 根据模型能力决定返回文字还是音频 Blob
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { transcribeAudio } from '@/services/api/asr'
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

/**
 * 获取 SpeechRecognition 构造函数
 *
 * 处理浏览器前缀兼容性
 */
function getSpeechRecognition(): SpeechRecognitionConstructor | null {
  if (typeof window === 'undefined') return null

  return window.SpeechRecognition || window.webkitSpeechRecognition || null
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
   * - browser：浏览器原生 SpeechRecognition（实时）
   * - server-asr：服务端 ASR 降级（浏览器识别不可用时自动切换，整段转写）
   */
  const [mode, setMode] = useState<'browser' | 'server-asr'>('browser')

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const isRecordingRef = useRef(false)
  const isManualStopRef = useRef(false)
  const durationTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const isSpeechRecognitionSupported = Boolean(getSpeechRecognition())
  const isMediaRecorderSupported =
    typeof window !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    typeof MediaRecorder !== 'undefined' &&
    MediaRecorder.isTypeSupported('audio/webm')

  const isSupported = supportsAudio ? isMediaRecorderSupported : isSpeechRecognitionSupported

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
   * 启动服务端 ASR 降级录音
   *
   * 当浏览器 Web Speech API 不可用（network/service-not-allowed 错误）时，
   * 自动切换到 MediaRecorder 录音 + 后端 ASR 转写模式。
   * 录音停止时上传音频到后端，转写结果通过 onTranscriptionComplete 回调返回。
   */
  const startServerASRFallback = useCallback(async () => {
    setMode('server-asr')
    setTranscript('')
    setState('transcribing')

    // 启动录音计时器
    if (durationTimerRef.current) {
      clearInterval(durationTimerRef.current)
    }
    setRecordingDuration(0)
    durationTimerRef.current = setInterval(() => {
      setRecordingDuration((prev) => prev + 1)
    }, 1000)

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream

      const mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data)
        }
      }

      mediaRecorder.onstop = async () => {
        const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
        audioChunksRef.current = []

        try {
          const result = await transcribeAudio(audioBlob, 'audio/webm')
          if (result?.text) {
            onTranscriptionComplete?.(result.text)
          } else {
            // 后端 ASR 未配置，友好提示
            onError?.({
              type: 'not_supported',
              message: '未配置语音转文字服务，请联系管理员启用 ASR',
            })
          }
        } catch {
          onError?.({
            type: 'transcription_failed',
            message: '语音转文字失败，请重试',
          })
        } finally {
          setState('idle')
        }
      }

      mediaRecorderRef.current = mediaRecorder
      mediaRecorder.start()
      setState('recording')
      isRecordingRef.current = true
    } catch (_err) {
      handleError('permission_denied', '无法访问麦克风，请检查权限设置')
    }
  }, [durationTimerRef, onTranscriptionComplete, onError, handleError])

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
      if (isManualStopRef.current) {
        const stopRelatedErrors = ['aborted', 'no-speech', 'network', 'service-not-allowed']
        if (stopRelatedErrors.includes(event.error)) {
          return
        }
      }

      let errorMessage = '语音识别失败'
      let errorType: VoiceInputError['type'] = 'transcription_failed'
      let shouldFallbackToServer = false

      switch (event.error) {
        case 'not-allowed':
        case 'permission-denied':
          errorMessage = '麦克风权限被拒绝，请在浏览器设置中允许访问麦克风'
          errorType = 'permission_denied'
          break
        case 'no-speech':
          return
        case 'audio-capture':
          errorMessage = '无法捕获音频，请检查麦克风设备'
          break
        case 'network':
          // 浏览器云端语音服务不可达，自动降级到服务端 ASR
          shouldFallbackToServer = true
          break
        case 'aborted':
          return
        case 'service-not-allowed':
          // 语音识别服务不可用，自动降级到服务端 ASR
          shouldFallbackToServer = true
          break
      }

      if (shouldFallbackToServer) {
        // 停止浏览器识别，切换到服务端 ASR 降级模式
        if (recognitionRef.current) {
          try {
            recognitionRef.current.stop()
          } catch {
            // 忽略停止错误
          }
        }
        startServerASRFallback()
        return
      }

      handleError(errorType, errorMessage)
    }

    return recognition
  }, [language, continuous, handleError, onTranscriptionComplete, onInterimResult, startServerASRFallback])

  /**
   * 开始录音
   */
  const startRecording = useCallback(async () => {
    setError(null)
    setTranscript('')
    setRecordingDuration(0)
    setMode('browser')
    isManualStopRef.current = false

    // 启动录音计时器（两种模式通用）
    if (durationTimerRef.current) {
      clearInterval(durationTimerRef.current)
    }
    setRecordingDuration(0)
    durationTimerRef.current = setInterval(() => {
      setRecordingDuration((prev) => prev + 1)
    }, 1000)

    if (supportsAudio) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        streamRef.current = stream

        const mediaRecorder = new MediaRecorder(stream, {
          mimeType: 'audio/webm',
        })

        mediaRecorder.ondataavailable = (event) => {
          if (event.data.size > 0) {
            audioChunksRef.current.push(event.data)
          }
        }

        mediaRecorder.onstop = () => {
          const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
          onRecordingComplete?.(audioBlob)
          audioChunksRef.current = []
          setState('idle')
        }

        mediaRecorderRef.current = mediaRecorder
        mediaRecorder.start()
        setState('recording')
        isRecordingRef.current = true
      } catch (_err) {
        handleError('permission_denied', '无法访问麦克风，请检查权限设置')
      }
    } else {
      const recognition = initSpeechRecognition()
      if (!recognition) return

      recognitionRef.current = recognition
      recognition.start()
      setState('recording')
      isRecordingRef.current = true
    }
  }, [supportsAudio, handleError, initSpeechRecognition, onRecordingComplete])

  /**
   * 停止录音
   */
  const stopRecording = useCallback(() => {
    isManualStopRef.current = true
    cleanup()
    setState('idle')
  }, [cleanup])

  // 组件卸载时清理资源
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
    isSupported,
    isSpeechRecognitionSupported,
    isMediaRecorderSupported,
  }
}
