/**
 * 管道配置 API 服务测试
 *
 * 覆盖 P7 前端数据层：
 * - getPipelineConfig：GET /api/v1/config/pipelines/{name}（返回 {name, data, etag}）
 * - savePipelineConfig：PUT /api/v1/config/pipelines/{name}（body {data}，返回 {name, etag}）
 *
 * 测试策略：Mock 仅传输层（apiClient），被测服务本身及其解析逻辑真实运行。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock 传输层（axios 实例），仅拦截 HTTP，不替代被测服务的解析逻辑。
vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(),
    put: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'
import {
  getPipelineConfig,
  savePipelineConfig,
} from '@/services/api/pipelineConfig'

const mockGet = vi.mocked(apiClient.get)
const mockPut = vi.mocked(apiClient.put)

/** 构造一个 axios 风格的响应对象。 */
function axResponse<T>(data: T): { data: T } {
  return { data }
}

describe('getPipelineConfig — GET 管道配置', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('调用 /api/v1/config/pipelines/{name} 并返回 {name, data, etag}', async () => {
    const pipelineData = {
      name: 'agentos_agent',
      input_routes: [{ name: 'tool_execute', target: 'core', plugins: ['tool_schema'], priority: 10 }],
    }
    mockGet.mockResolvedValue(axResponse({ name: 'default', data: pipelineData, etag: 'etag-1' }))

    const result = await getPipelineConfig('default')

    expect(mockGet).toHaveBeenCalledWith('/api/v1/config/pipelines/default')
    expect(result.name).toBe('default')
    expect(result.data).toEqual(pipelineData)
    expect(result.etag).toBe('etag-1')
  })

  it('data 为空对象时正常返回（管道配置为空）', async () => {
    mockGet.mockResolvedValue(axResponse({ name: 'empty', data: {}, etag: 'etag-e' }))

    const result = await getPipelineConfig('empty')

    expect(result.data).toEqual({})
  })
})

describe('savePipelineConfig — PUT 管道配置', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('调用 PUT /api/v1/config/pipelines/{name}，body 为 {data}，返回 {name, etag}', async () => {
    const pipelineData = {
      name: 'agentos_agent',
      input_routes: [],
    }
    mockPut.mockResolvedValue(axResponse({ name: 'default', etag: 'etag-new' }))

    const result = await savePipelineConfig('default', pipelineData)

    expect(mockPut).toHaveBeenCalledWith('/api/v1/config/pipelines/default', {
      data: pipelineData,
    })
    expect(result.name).toBe('default')
    expect(result.etag).toBe('etag-new')
  })

  it('非 2xx 错误原样透传（如 404 管道不存在）', async () => {
    const notFound = Object.assign(new Error('Request failed with status code 404'), {
      response: { status: 404, data: { message: 'pipeline config not found' } },
    })
    mockPut.mockRejectedValue(notFound)

    await expect(savePipelineConfig('nope', {})).rejects.toBe(notFound)
  })
})
