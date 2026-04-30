/**
 * 语音输入 Hook
 *
 * 提供语音识别和音频录制功能：
 * - 使用 Web Speech API (SpeechRecognition) 进行实时语音转文字
 * - 使用 MediaRecorder 录制 WebM 音频
 * - 根据模型能力决定返回文字还是音频 Blob
 */

import { useCallback, useEffect, useRef, useState } from 'react'
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
    onError,
  } = options

  const [state, setState] = useState<VoiceInputState>('idle')
  const [transcript, setTranscript] = useState('')
  const [error, setError] = useState<VoiceInputError | null>(null)

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const isRecordingRef = useRef(false)
  const isManualStopRef = useRef(false)

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

      setTranscript((prev) => {
        const newText = finalTranscript || interimTranscript
        return newText || prev
      })

      if (finalTranscript) {
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
          errorMessage = '网络错误，语音识别服务不可用'
          break
        case 'aborted':
          return
        case 'service-not-allowed':
          errorMessage = '语音识别服务不可用，请检查浏览器设置'
          break
      }

      handleError(errorType, errorMessage)
    }

    return recognition
  }, [language, continuous, handleError, onTranscriptionComplete])

  /**
   * 开始录音
   */
  const startRecording = useCallback(async () => {
    setError(null)
    setTranscript('')
    isManualStopRef.current = false

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
    error,
    startRecording,
    stopRecording,
    isSupported,
    isSpeechRecognitionSupported,
    isMediaRecorderSupported,
  }
}
