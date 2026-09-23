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

/** 未知错误收窄：带 axios 形态 response 的错误对象 */
function isErrorWithResponse(error: unknown): error is ErrorWithResponse {
  return (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    typeof (error as ErrorWithResponse).response === 'object'
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** 从后端结构化错误体提取用户可读原因（{code, message}）；缺失时回退 HTTP 状态码 */
function extractFailureReason(response: ErrorWithResponse['response']): {
  message: string
  code?: string
} {
  const status = response?.status
  const body = response?.data
  const code = isRecord(body) && typeof body.code === 'string' ? body.code : undefined
  const backendMessage =
    isRecord(body) && typeof body.message === 'string' && body.message ? body.message : undefined
  return { message: backendMessage ?? `HTTP ${status ?? 'unknown'}`, code }
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
    // 503 = 后端 ASR 未配置，静默返回 null（不触发全局报错）
    if (isErrorWithResponse(error) && error.response?.status === 503) {
      return null
    }
    // HTTP 错误 → 抛带结构化原因的 TranscriptionError（横幅补原因）
    if (isErrorWithResponse(error)) {
      const { message, code } = extractFailureReason(error.response)
      throw new TranscriptionError(message, code)
    }
    throw error
  }
}
