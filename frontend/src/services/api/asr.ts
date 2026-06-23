/**
 * 语音识别（ASR）API 服务
 *
 * 调用后端 /api/v1/audio/transcriptions 端点，将音频转写为文本。
 * 用于浏览器 Web Speech API 不可用（如 Edge/Chrome 云端服务不可达报 network 错误）时的降级。
 *
 * 暴露接口：
 * - transcribeAudio(blob, mimeType): 转写结果 | null - 音频转文字
 */

import apiClient from '@/services/api/client'

/** 转写结果 */
export interface TranscriptionResult {
  /** 转写得到的文本 */
  text: string
}

/**
 * 将音频 Blob 转写为文本
 *
 * @param blob 音频二进制数据
 * @param mimeType 音频 MIME 类型（如 'audio/webm'）
 * @returns 转写结果；后端未配置 ASR（503）时返回 null，前端据此提示降级不可用
 *
 * BUG-FIX: 静默处理 503（ASR 未配置），避免全局错误拦截器报错
 */
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
      '/api/v1/audio/transcriptions',
      formData,
      {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 60000,
      },
    )
    return response.data
  } catch (error: any) {
    // 503 = 后端 ASR 未配置，静默返回 null（不触发全局报错）
    if (error?.response?.status === 503) {
      return null
    }
    throw error
  }
}
