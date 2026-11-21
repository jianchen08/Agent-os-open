/**
 * 原生语音识别（Android 壳 ShellSpeech 桥）错误决策与会话装配。
 *
 * 通道语义：壳 WebView 内原生优先（Android 系统 SpeechRecognizer，实时
 * partial/final 上屏，Java 侧静默续听），探测不可用或未产出文本的运行时错误
 * → 调用方回退 server-asr；已产出文本后的失败只报错终止（回退会重启录音，
 * 丢已识别文本）。错误码词表与 Web Speech 同形但独立映射——浏览器路径的
 * decideRecognitionError 不受本模块影响（纯浏览器行为零改动）。
 */

import type { ShellSpeechPlugin } from '@/services/shellPlugins'
import type { VoiceInputError } from '@/types/voiceInput'

/**
 * 原生识别错误处置决策：
 * - 'ignore'：静默（手动停止相关错误 / Java 侧自动重启续听的暂态错误）
 * - 'fallback'：回退 server-asr（会话未产出任何文本，切换零丢失）
 * - 错误对象：终止会话并向用户报错
 */
export type NativeSpeechErrorDecision =
  | 'ignore'
  | 'fallback'
  | { errorMessage: string; errorType: VoiceInputError['type'] }

/**
 * 纯函数：ShellSpeech 错误码 + 是否已产出文本 → 处置决策。
 *
 * 词表来源 = Java 桥 mapErrorCode（Android ERROR_* → Web Speech 同形码）。
 * 未产出文本的失败一律回退（含未知码——启动报错自动回退是本通道的契约）；
 * 已产出文本后回退会丢已识别内容，故只报错。
 */
export function decideNativeSpeechError(
  code: string,
  producedTranscript: boolean,
): NativeSpeechErrorDecision {
  switch (code) {
    case 'no-speech':
    case 'no-match':
    case 'aborted':
    case 'busy':
      // 暂态/静音/停止相关：Java 侧重启续听，不惊动调用方
      return 'ignore'
    case 'not-allowed':
      return {
        errorMessage: '语音识别服务权限不足，无法启动识别',
        errorType: 'permission_denied',
      }
    case 'audio-capture':
      return {
        errorMessage: '无法捕获音频，请检查麦克风设备',
        errorType: 'transcription_failed',
      }
    default:
      return producedTranscript
        ? { errorMessage: '语音识别失败', errorType: 'transcription_failed' }
        : 'fallback'
  }
}

/** 会话事件处理器（调用方=useVoiceInput，负责通道切换与状态机） */
export interface NativeSpeechSessionHandlers {
  /** 实时中间结果（未确认文本，驱动输入框实时显示） */
  onInterim(text: string): void
  /** 已确认最终文本（持续识别下每段落一次） */
  onFinal(text: string): void
  /** 回退信号：探测不可用 / start 失败 / 未产出文本的运行时错误 */
  onFallback(): void
  /** 终止信号：已产出文本后的失败，附带面向用户的错误 */
  onFatal(error: VoiceInputError): void
  /** 会话收尾（停止后结果落地 / 停止链路异常），调用方置 idle */
  onSettled(): void
}

export interface NativeSpeechSession {
  /** 优雅停止：等 Java 侧 onResults/onError 落最终结果（此后会话收尾） */
  stop(): void
  /** 取消：断开监听并丢弃（不提交任何在途结果） */
  cancel(): void
}

/**
 * 装配一次原生识别会话：探测 → 挂事件监听 → start。
 *
 * 返回 null = native 不可行（探测不可用 / available() 抛错 / start 拒绝 /
 * 装配链路异常），调用方回退 server-asr。事件回调内置防重入：stop 后丢弃
 * 迟到 partial；cancel 后丢弃一切事件。
 */
export async function startNativeSpeech(
  bridge: ShellSpeechPlugin,
  language: string,
  handlers: NativeSpeechSessionHandlers,
): Promise<NativeSpeechSession | null> {
  let probe: Awaited<ReturnType<ShellSpeechPlugin['available']>> | null = null
  try {
    probe = await bridge.available()
  } catch {
    // 块状处理登记：探测异常按「原生不可用」归一回退口（返回 null → 调用方切
    // server-asr），null 为本函数既定回退信号而非正常数据伪装。
  }
  if (!probe?.available) return null

  let sawTranscript = false
  let stopping = false
  let unwired = false
  const handles = await Promise.all([
    bridge.addListener('speechPartial', (data) => {
      if (unwired || stopping) return
      const text = typeof data?.transcript === 'string' ? data.transcript : ''
      if (!text) return
      sawTranscript = true
      handlers.onInterim(text)
    }),
    bridge.addListener('speechFinal', (data) => {
      if (unwired) return
      const text = typeof data?.transcript === 'string' ? data.transcript : ''
      if (text) {
        sawTranscript = true
        handlers.onFinal(text)
      }
      if (stopping) {
        // 停止后的最终结果（或空结果）：会话收尾
        unwire()
        handlers.onSettled()
      }
    }),
    bridge.addListener('speechError', (data) => {
      if (unwired) return
      const code = typeof data?.code === 'string' ? data.code : 'client'
      const decision = decideNativeSpeechError(code, sawTranscript)
      if (decision === 'ignore' && !stopping) return
      unwire()
      if (decision === 'ignore') {
        // 停止链路中的可忽略错误：按正常收尾处理
        handlers.onSettled()
        return
      }
      if (decision === 'fallback') {
        handlers.onFallback()
        return
      }
      handlers.onFatal({ type: decision.errorType, message: decision.errorMessage })
    }),
  ])

  const unwire = () => {
    if (unwired) return
    unwired = true
    for (const handle of handles) {
      void Promise.resolve(handle.remove()).catch(() => {
        // 桥侧移除失败不影响本地静默
      })
    }
  }

  try {
    await bridge.start({ language })
  } catch {
    unwire()
    return null
  }

  return {
    stop() {
      if (stopping || unwired) return
      stopping = true
      bridge
        .stop()
        .catch(() => {
          // 停止指令失败：不再等待结果事件，直接收尾
          unwire()
          handlers.onSettled()
        })
    },
    cancel() {
      unwire()
      bridge.cancel().catch(() => {
        // 取消指令失败不影响本地状态（监听已断开）
      })
    },
  }
}
