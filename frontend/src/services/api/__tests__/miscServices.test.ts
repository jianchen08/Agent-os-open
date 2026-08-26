/**
 * 小型 API 服务测试（ASR / 全局搜索 / 会话 Token 用量 / Payload 诊断 / 评估指标）
 *
 * 覆盖五个此前无测试的端点封装：
 * - asr.transcribeAudio：multipart 上传、503 静默降级 null、其余错误抛出
 * - search.searchGlobal：查询参数透传 + requestWithRetry 包装
 * - sessions：会话 Token 总量 / 上下文 Token 用量（可选父记录参数）
 * - llmPayload：payload 快照列表 / 单文件读取
 * - evaluationMetrics：指标列表字段归一化、单条详情、删除（失败降级 false）
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { transcribeAudio } from '@/services/api/asr'
import { searchGlobal } from '@/services/api/search'
import { getSessionTotalTokenUsage, getContextTokenUsage } from '@/services/api/sessions'
import { getPayloadDiagList, getPayloadDiagFile } from '@/services/api/llmPayload'
import {
  getEvaluationMetrics,
  getEvaluationMetric,
  deleteEvaluationMetric,
} from '@/services/api/evaluationMetrics'

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

describe('会话 Token 用量 API - sessions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('getSessionTotalTokenUsage 请求总量端点', async () => {
    const resp = {
      session_id: 's1',
      total_tokens: 100,
      prompt_tokens: 60,
      completion_tokens: 40,
      request_count: 2,
    }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

    const result = await getSessionTotalTokenUsage('s1')

    expect(result.total_tokens).toBe(100)
    expect(apiClient.get).toHaveBeenCalledWith(
      '/ext/monitoring/sessions/s1/total-token-usage',
    )
  })

  it('getContextTokenUsage 无父记录时不带查询参数', async () => {
    const resp = { current_context_tokens: 50, is_estimated: false, model: 'm' }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

    const result = await getContextTokenUsage('s1')

    expect(result.current_context_tokens).toBe(50)
    expect(apiClient.get).toHaveBeenCalledWith(
      '/ext/monitoring/sessions/s1/context-token-usage',
      { params: undefined },
    )
  })

  it('getContextTokenUsage 带父记录时透传参数', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({}))

    await getContextTokenUsage('s1', 'p1')

    expect(apiClient.get).toHaveBeenCalledWith(
      '/ext/monitoring/sessions/s1/context-token-usage',
      { params: { parent_execution_record_id: 'p1' } },
    )
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

  it('getEvaluationMetrics 归一化缺失字段', async () => {
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

    expect(result.total).toBe(2)
    expect(result.metrics[0].category).toBe('quality')
    expect(result.metrics[1].category).toBe('legacy_type')
    expect(result.metrics[1].usage_count).toBe(0)
    expect(result.metrics[1].success_count).toBe(0)
    expect(result.metrics[1].created_at).toBe('')
    expect(apiClient.get).toHaveBeenCalledWith('/ext/evaluation_service/metrics', {
      params: { skip: 0, limit: 20 },
    })
  })

  it('getEvaluationMetric 请求单条详情', async () => {
    const metric = { id: 'm1', name: 'correctness', description: 'd' }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(metric))

    const result = await getEvaluationMetric('m1')

    expect(result.id).toBe('m1')
    expect(apiClient.get).toHaveBeenCalledWith('/ext/evaluation_service/metrics/m1')
  })

  it('deleteEvaluationMetric 成功返回 true', async () => {
    vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({}))

    const result = await deleteEvaluationMetric('m1')

    expect(result).toBe(true)
    expect(apiClient.delete).toHaveBeenCalledWith('/ext/evaluation_service/metrics/m1')
  })

  it('deleteEvaluationMetric 失败降级返回 false', async () => {
    vi.mocked(apiClient.delete).mockRejectedValueOnce(new Error('Network Error'))

    const result = await deleteEvaluationMetric('m1')

    expect(result).toBe(false)
  })
})
