// @feature FP-T12 前端组件补测 | @ci: frontend-test
/**
 * ASR API 服务测试（66ca4af82 语音服务端 ASR 直录配套）：
 * - 成功：FormData 出站（文件名按 MIME 推扩展名、language=zh-CN、multipart 头 + 60s 超时）
 * - 静默面：HTTP 503 与拦截器重抛形态 code=asr_not_configured → null（走设置引导）
 * - 结构化失败：response.data / details 双形态提码提词 → TranscriptionError
 * - 回退面：无结构化体 → HTTP 状态码 / 错误 message 兜底；非对象错误原样重抛
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockApiPost = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => {
  const client = {
    get: vi.fn(),
    post: (...args: unknown[]) => mockApiPost(...args),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  return { default: client, apiClient: client }
})

import { TranscriptionError, transcribeAudio } from '@/services/api/asr'
import apiClient from '@/services/api/client'
import { MULTIMODAL_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'

const makeBlob = (): Blob => new Blob(['fake-audio'], { type: 'audio/webm' })

beforeEach(() => {
  mockApiPost.mockReset()
})

describe('transcribeAudio — 成功与出站形态', () => {
  it('成功转写：post 携带端点/FormData（文件名扩展与语言）/multipart 头/60s 超时', async () => {
    mockApiPost.mockResolvedValue({ data: { text: '你好灵汐' } })
    const result = await transcribeAudio(makeBlob(), 'audio/webm')
    expect(result).toEqual({ text: '你好灵汐' })
    expect(mockApiPost).toHaveBeenCalledTimes(1)
    const [endpoint, formData, config] = mockApiPost.mock.calls[0] as [
      string,
      FormData,
      { headers: Record<string, string>; timeout: number },
    ]
    expect(endpoint).toBe(MULTIMODAL_SERVICE_ENDPOINTS.mm_asr_transcriptions)
    expect(formData.get('language')).toBe('zh-CN')
    const file = formData.get('file') as File
    expect(file.name).toBe('audio.webm')
    expect(config.headers['Content-Type']).toBe('multipart/form-data')
    expect(config.timeout).toBe(60000)
  })

  it('MIME 带参数时文件名扩展取主段；未知子类型回退 webm', async () => {
    mockApiPost.mockResolvedValue({ data: { text: 'x' } })
    await transcribeAudio(makeBlob(), 'audio/mpeg;codecs=opus')
    expect((mockApiPost.mock.calls[0][1] as FormData).get('file')).toMatchObject({ name: 'audio.mpeg' })
    await transcribeAudio(makeBlob(), 'audio')
    expect((mockApiPost.mock.calls[1][1] as FormData).get('file')).toMatchObject({ name: 'audio.webm' })
  })
})

describe('transcribeAudio — 未配置静默面', () => {
  it('HTTP 503 → null（不抛错，走设置引导）', async () => {
    mockApiPost.mockRejectedValue({ response: { status: 503 } })
    expect(await transcribeAudio(makeBlob(), 'audio/webm')).toBeNull()
  })

  it('拦截器重抛形态 code=asr_not_configured（无 response）→ null', async () => {
    const err = new Error('service unavailable')
    ;(err as Error & { details?: unknown }).details = { code: 'asr_not_configured' }
    mockApiPost.mockRejectedValue(err)
    expect(await transcribeAudio(makeBlob(), 'audio/webm')).toBeNull()
  })
})

describe('transcribeAudio — 结构化失败提因', () => {
  it('response.data 结构化体：message 与 code 进 TranscriptionError', async () => {
    mockApiPost.mockRejectedValue({ response: { status: 500, data: { code: 'asr_failed', message: '模型炸了' } } })
    await expect(transcribeAudio(makeBlob(), 'audio/webm')).rejects.toMatchObject({
      name: 'TranscriptionError',
      message: '模型炸了',
      code: 'asr_failed',
    })
  })

  it('拦截器重抛形态 details 结构化体同样提因', async () => {
    const err = new Error('请求失败')
    ;(err as Error & { details?: unknown }).details = { code: 'asr_timeout', message: '识别超时' }
    mockApiPost.mockRejectedValue(err)
    await expect(transcribeAudio(makeBlob(), 'audio/webm')).rejects.toMatchObject({
      message: '识别超时',
      code: 'asr_timeout',
    })
  })

  it('无结构化体：回退 HTTP 状态码文案', async () => {
    mockApiPost.mockRejectedValue({ response: { status: 500 } })
    await expect(transcribeAudio(makeBlob(), 'audio/webm')).rejects.toMatchObject({
      message: 'HTTP 500',
      code: undefined,
    })
  })

  it('无结构化体且无 status：回退错误 message', async () => {
    mockApiPost.mockRejectedValue(new Error('网络断开'))
    await expect(transcribeAudio(makeBlob(), 'audio/webm')).rejects.toMatchObject({ message: '网络断开' })
  })

  it('非对象错误（字符串）：原样重抛不包装', async () => {
    mockApiPost.mockRejectedValue('裸字符串错误')
    await expect(transcribeAudio(makeBlob(), 'audio/webm')).rejects.toBe('裸字符串错误')
  })
})

describe('TranscriptionError', () => {
  it('错误名与 code 字段按构造参数落位', () => {
    const err = new TranscriptionError('boom', 'asr_failed')
    expect(err.name).toBe('TranscriptionError')
    expect(err.code).toBe('asr_failed')
    expect(new TranscriptionError('boom').code).toBeUndefined()
    void apiClient
  })
})
