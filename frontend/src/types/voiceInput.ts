/**
 * 语音输入类型定义
 *
 * 定义语音输入相关的类型接口
 */

/**
 * 语音输入状态
 */
export type VoiceInputState = 'idle' | 'recording' | 'transcribing'

/**
 * 语音输入错误类型
 */
export type VoiceInputErrorType =
  | 'not_supported'
  | 'permission_denied'
  | 'recording_failed'
  | 'transcription_failed'
  | 'unknown'

/**
 * 语音输入错误信息
 */
export interface VoiceInputError {
  /** 错误类型 */
  type: VoiceInputErrorType
  /** 错误消息 */
  message: string
}

/**
 * useVoiceInput Hook 配置选项
 */
export interface UseVoiceInputOptions {
  /** 模型是否支持音频输入 */
  supportsAudio?: boolean
  /** 语言代码（如 'zh-CN', 'en-US'） */
  language?: string
  /** 是否连续识别 */
  continuous?: boolean
  /**
   * 优先浏览器原生 SpeechRecognition 实时识别（默认 false）
   *
   * 默认录音链路 = getUserMedia + MediaRecorder → 服务端 ASR 转写；仅显式开启时
   * 才用浏览器识别（Electron/国内 Chrome 无云端识别服务，默认路径会空转丢话）。
   */
  preferBrowser?: boolean
  /** 录音时长上限（秒），到点自动停止并走当前通道后续流程；默认 60 */
  maxDurationSeconds?: number
  /** 录音完成回调 */
  onRecordingComplete?: (blob: Blob) => void
  /** 转写完成回调（已确认的最终文字） */
  onTranscriptionComplete?: (text: string) => void
  /** 实时临时识别结果回调（未确认的中间文字，用于实时显示） */
  onInterimResult?: (interim: string) => void
  /** 错误回调 */
  onError?: (error: VoiceInputError) => void
}

/**
 * useVoiceInput Hook 返回值
 */
export interface UseVoiceInputReturn {
  /** 当前状态 */
  state: VoiceInputState
  /** 是否正在录音 */
  isRecording: boolean
  /** 是否正在转写 */
  isTranscribing: boolean
  /** 实时转写文本 */
  transcript: string
  /** 当前录音时长（秒），仅在录音中递增 */
  recordingDuration: number
  /** 当前识别模式：native 壳原生识别；browser 浏览器原生识别；server-asr 服务端 ASR 降级 */
  mode: 'native' | 'browser' | 'server-asr'
  /** 错误信息 */
  error: VoiceInputError | null
  /** 开始录音（按能力选择通道：音频模型 → 录音落附件；默认 → 服务端 ASR） */
  startRecording: () => Promise<void>
  /** 停止录音 */
  stopRecording: () => void
  /** 显式启动服务端 ASR 录音（MediaRecorder → 停止后整段转写） */
  startServerASR: () => Promise<void>
  /** 取消录音/转写：停止并丢弃，不触发转写与附件回调 */
  cancelRecording: () => void
  /** 是否支持语音输入 */
  isSupported: boolean
  /** 是否支持语音识别 */
  isSpeechRecognitionSupported: boolean
  /** 是否支持音频录制 */
  isMediaRecorderSupported: boolean
}

/**
 * VoiceInputButton 组件属性
 */
export interface VoiceInputButtonProps {
  /** 是否禁用 */
  disabled?: boolean
  /** 当前状态 */
  state?: VoiceInputState
  /** 错误信息 */
  error?: VoiceInputError | null
  /** 点击回调（鼠标路径：点按开始/停止切换） */
  onClick?: () => void
  /** 按住开始（触屏路径：pointerdown 且非鼠标——微信式按住说话） */
  onPressStart?: () => void
  /** 松开停止（触屏路径：pointerup/pointercancel——松手即转写落输入框） */
  onPressEnd?: () => void
  /** 自定义类名 */
  className?: string
  /** 录音时长（秒），用于按钮动画节律，不影响布局 */
  recordingDuration?: number
}

/**
 * Web Speech API 类型定义
 *
 * 浏览器可能使用 webkit 前缀
 */

export interface SpeechRecognitionEvent extends Event {
  readonly resultIndex: number
  readonly results: SpeechRecognitionResultList
}

export interface SpeechRecognitionErrorEvent extends Event {
  readonly error: string
  readonly message: string
}

export interface SpeechRecognitionInstance extends EventTarget {
  continuous: boolean
  interimResults: boolean
  lang: string
  maxAlternatives: number
  onaudioend: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onaudiostart: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onend: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onerror: ((this: SpeechRecognitionInstance, ev: SpeechRecognitionErrorEvent) => void) | null
  onnomatch: ((this: SpeechRecognitionInstance, ev: SpeechRecognitionEvent) => void) | null
  onresult: ((this: SpeechRecognitionInstance, ev: SpeechRecognitionEvent) => void) | null
  onsoundend: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onsoundstart: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onspeechend: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onspeechstart: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  onstart: ((this: SpeechRecognitionInstance, ev: Event) => void) | null
  abort(): void
  start(): void
  stop(): void
}

export interface SpeechRecognitionConstructor {
  new (): SpeechRecognitionInstance
}

declare global {
  interface Window {
    SpeechRecognition?: SpeechRecognitionConstructor
    webkitSpeechRecognition?: SpeechRecognitionConstructor
  }
}
