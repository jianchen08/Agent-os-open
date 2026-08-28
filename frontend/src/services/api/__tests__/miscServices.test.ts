// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 小型 API 服务测试（ASR / 全局搜索 / Payload 诊断 / 评估指标）
 *
 * 覆盖四个此前无测试的端点封装：
 * - asr.transcribeAudio：multipart 上传、503 静默降级 null、其余错误抛出
 * - search.searchGlobal：查询参数透传 + requestWithRetry 包装
 * - llmPayload：payload 快照列表 / 单文件读取
 * - evaluationMetrics：指标列表字段归一化
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { transcribeAudio } from '@/services/api/asr'
import { searchGlobal } from '@/services/api/search'
import { getPayloadDiagList, getPayloadDiagFile } from '@/services/api/llmPayload'
import { getEvaluationMetrics } from '@/services/api/evaluationMetrics'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

describe('ASR API - transcribeAudio', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('上传音频并返回转写文本', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ text: '你好' }))

    const result = await transcribeAudio(new Blob(['audio'], { type: 'audio/webm' }), 'audio/webm')

    expect(result?.text).toBe('你好')
    expect(apiClient.post).toHaveBeenCalledWith(
      '/ext/multimodal_service/audio/transcriptions',
      expect.any(FormData),
      { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 60000 },
    )
  })

  it('503（ASR 未配置）时静默返回 null', async () => {
    vi.mocked(apiClient.post).mockRejectedValueOnce({ response: { status: 503 } })

    const result = await transcribeAudio(new Blob(['a']), 'audio/webm')

    expect(result).toBeNull()
  })

  it('其他错误原样抛出', async () => {
    vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('Network Error'))

    await expect(transcribeAudio(new Blob(['a']), 'audio/webm')).rejects.toThrow('Network Error')
  })
})

describe('全局搜索 API - searchGlobal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('默认参数 all/limit=20', async () => {
    const resp = { query: 'q', type: 'all', sessions: [], messages: [] }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

    const result = await searchGlobal('q')

    expect(result).toEqual(resp)
    expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/search', {
      params: { q: 'q', type: 'all', limit: 20 },
    })
  })

  it('自定义类型与 limit 透传', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({}))

    await searchGlobal('q', 'message', 5)

    expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/search', {
      params: { q: 'q', type: 'message', limit: 5 },
    })
  })
})

describe('Payload 诊断 API - llmPayload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('getPayloadDiagList 请求列表端点', async () => {
    const resp = { items: [{ name: 'f1', ts: 1, model: 'm', msgs_hash: 'h', msg_count: 2 }], total: 1 }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

    const result = await getPayloadDiagList()

    expect(result.total).toBe(1)
    expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/payload-diag')
  })

  it('getPayloadDiagFile 按名称读取快照', async () => {
    const resp = { name: 'f1', content: '{"model":"m"}' }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

    const result = await getPayloadDiagFile('f1')

    expect(result.content).toContain('model')
    expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/payload-diag/file', {
      params: { name: 'f1' },
    })
  })
})

describe('评估指标 API - evaluationMetrics', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it("getEvaluationMetrics 直接透传（S3 改判：缺失字段不再伪造空串/0，由后端契约保证全量）", async () => {
    const resp = {
      metrics: [
        {
          id: 'm1',
          name: 'correctness',
          description: 'd',
          category: 'quality',
          evaluator_type: 'llm',
          evaluator_id: 'e1',
          level: 1,
          is_red_line: false,
          default_weight: 1,
          source: 'builtin',
          status: 'active',
          usage_count: 3,
          success_count: 2,
          created_at: 't',
        },
        // 旧后端缺 category/usage_count/success_count/created_at
        {
          id: 'm2',
          name: 'legacy',
          description: 'd',
          metric_type: 'legacy_type',
          evaluator_type: 'rule',
          evaluator_id: 'e2',
          level: 2,
          is_red_line: true,
          default_weight: 2,
          source: 'plugin',
          status: 'active',
        },
      ],
      total: 2,
    }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

    const result = await getEvaluationMetrics({ skip: 0, limit: 20 })

    // 直接透传：响应对象与请求方原样一致（不做 ''/0 补齐）
    expect(result.total).toBe(2)
    expect(result.metrics[0]).toBe(resp.metrics[0])
    expect(result.metrics[1]).toBe(resp.metrics[1])
    expect(apiClient.get).toHaveBeenCalledWith('/ext/evaluation_service/metrics', {
      params: { skip: 0, limit: 20 },
    })
  })
})
