/** 语音识别（ASR）API 服务 调用后端 /api/v1/audio/transcriptions 端点，将音频转写为文本。 */

import apiClient from '@/services/api/client'
import { MULTIMODAL_SERVICE_ENDPOINTS } from './endpoints.generated'

export interface TranscriptionResult {
  text: string
}

/** 转写失败错误：携带后端结构化错误体（code/message），供调用方在横幅中补原因 */
export class TranscriptionError extends Error {
  /** 后端错误码（如 asr_failed）；无结构化体时缺省 */
  readonly code?: string

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'TranscriptionError'
    this.code = code
  }
}

interface ErrorWithResponse {
  response?: { status?: number; data?: unknown }
}

/** 错误对象面：axios 原始形态（response）与 apiClient 拦截器重抛形态（code/message/details） */
interface ErrorWithMeta extends ErrorWithResponse {
  code?: string
  message?: string
  details?: unknown
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** 客户端错误面收窄：拦截器对任意 Error 实例附加 code/details 后重抛 */
function isErrorWithMeta(error: unknown): error is ErrorWithMeta {
  return typeof error === 'object' && error !== null
}

/** 后端结构化错误体：优先 response.data（axios 原始），其次 details（拦截器重抛形态） */
function errorBody(error: ErrorWithMeta): Record<string, unknown> | undefined {
  if (isRecord(error.response?.data)) return error.response.data
  if (isRecord(error.details)) return error.details
  return undefined
}

/** 未配置判定：HTTP 503，或后端结构化 code=asr_not_configured（插件 503 信封） */
function isNotConfigured(error: ErrorWithMeta): boolean {
  if (error.response?.status === 503) return true
  return errorBody(error)?.code === 'asr_not_configured'
}

/** 从后端结构化错误体提取用户可读原因（{code, message}）；缺失时回退错误 message / HTTP 状态码 */
function extractFailureReason(error: ErrorWithMeta): {
  message: string
  code?: string
} {
  const body = errorBody(error)
  const code = typeof body?.code === 'string' ? body.code : undefined
  const backendMessage =
    typeof body?.message === 'string' && body.message ? body.message : undefined
  const fallback = error.response?.status ? `HTTP ${error.response.status}` : error.message || '未知错误'
  return { message: backendMessage ?? fallback, code }
}

/** 将音频 Blob 转写为文本 静默处理 503（ASR 未配置），避免全局错误拦截器报错 */
export async function transcribeAudio(
  blob: Blob,
  mimeType: string,
): Promise<TranscriptionResult | null> {
  const formData = new FormData()
  // 文件名仅占位，后端按 MIME 判断
  const ext = mimeType.split('/')[1]?.split(';')[0] || 'webm'
  formData.append('file', blob, `audio.${ext}`)
  formData.append('language', 'zh-CN')

  try {
    const response = await apiClient.post<TranscriptionResult>(
      MULTIMODAL_SERVICE_ENDPOINTS.mm_asr_transcriptions,
      formData,
      {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 60000,
      },
    )
    return response.data
  } catch (error: unknown) {
    if (isErrorWithMeta(error)) {
      // 503 / asr_not_configured = 后端 ASR 未配置，静默返回 null（走到设置引导）
      if (isNotConfigured(error)) {
        return null
      }
      // 其余失败 → 抛带结构化原因的 TranscriptionError（横幅补原因）
      const { message, code } = extractFailureReason(error)
      throw new TranscriptionError(message, code)
    }
    throw error
  }
}
