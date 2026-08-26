/**
 * 配置管理 API 服务测试（LLM 配置域）
 *
 * 覆盖 /ext/llm_service/config/llm* 端点封装：LLM 配置、提供者类型、远端模型、
 * 模型 CRUD、默认配置、提供者 CRUD。所有函数走 requestWithRetry 包装，
 * 断言请求 URL/参数/载荷与响应解包。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as configApi from '@/services/api/config'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

describe('配置管理 API（LLM 配置域）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getLLMConfig - 获取 LLM 配置', () => {
    it('请求配置端点并解包', async () => {
      const resp = { models: {}, providers: {}, defaults: { chat: 'm1', tiers: {}, embedding: 'e1' } }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await configApi.getLLMConfig()

      expect(result.defaults.chat).toBe('m1')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/llm_service/config/llm')
    })

    it('启用重试时失败后重试成功', async () => {
      const resp = { models: {}, providers: {}, defaults: { chat: 'm1', tiers: {}, embedding: 'e1' } }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce(okResponse(resp))

      const result = await configApi.getLLMConfig({ retry: true, maxRetries: 2, retryDelay: 1 })

      expect(result.defaults.chat).toBe('m1')
      expect(apiClient.get).toHaveBeenCalledTimes(2)
    })
  })

  describe('getProviderTypes - 提供者类型清单', () => {
    it('请求 provider-types 端点', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ types: ['openai', 'deepseek'] }))

      const result = await configApi.getProviderTypes()

      expect(result.types).toContain('deepseek')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/llm_service/config/llm/provider-types')
    })
  })

  describe('getRemoteModels - 远端模型', () => {
    it('按 providerId 请求并解包', async () => {
      const resp = { provider: 'deepseek', models: [{ id: 'deepseek-chat', owned_by: 'deepseek' }] }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await configApi.getRemoteModels('deepseek')

      expect(result.models[0].id).toBe('deepseek-chat')
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/llm_service/config/llm/providers/deepseek/remote-models',
      )
    })
  })

  describe('getModels / getDefaults', () => {
    it('getModels 请求 models 端点', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ models: {} }))

      const result = await configApi.getModels()

      expect(result.models).toEqual({})
      expect(apiClient.get).toHaveBeenCalledWith('/ext/llm_service/config/llm/models')
    })

    it('getDefaults 请求 defaults 端点', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        okResponse({ chat: 'm1', tiers: { fast: 'm2' }, embedding: 'e1' }),
      )

      const result = await configApi.getDefaults()

      expect(result.tiers.fast).toBe('m2')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/llm_service/config/llm/defaults')
    })
  })

  describe('模型 CRUD', () => {
    it('addModel POST 模型配置', async () => {
      const config = { provider: 'openai', model_name: 'gpt-4o', display_name: 'GPT-4o' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ gpt4o: config }))

      const result = await configApi.addModel('gpt4o', config)

      expect(result.gpt4o).toEqual(config)
      expect(apiClient.post).toHaveBeenCalledWith('/ext/llm_service/config/llm/models', {
        models: { gpt4o: config },
      })
    })

    it('updateModel PUT 到模型子路径', async () => {
      vi.mocked(apiClient.put).mockResolvedValueOnce(okResponse({}))

      await configApi.updateModel('gpt4o', { display_name: 'GPT-4o 新' })

      expect(apiClient.put).toHaveBeenCalledWith('/ext/llm_service/config/llm/models/gpt4o', {
        config: { display_name: 'GPT-4o 新' },
      })
    })

    it('deleteModel DELETE 到模型子路径', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({}))

      await configApi.deleteModel('gpt4o')

      expect(apiClient.delete).toHaveBeenCalledWith('/ext/llm_service/config/llm/models/gpt4o')
    })
  })

  describe('提供者 CRUD', () => {
    it('updateProviderConfig PUT 并解包 providers', async () => {
      const providers = { deepseek: { type: 'deepseek', keys: [] } }
      vi.mocked(apiClient.put).mockResolvedValueOnce(okResponse({ providers }))

      const result = await configApi.updateProviderConfig('deepseek', { api_base: 'x' })

      expect(result).toEqual(providers)
      expect(apiClient.put).toHaveBeenCalledWith(
        '/ext/llm_service/config/llm/providers/deepseek',
        { config: { api_base: 'x' } },
      )
    })

    it('addProvider POST 并解包 providers', async () => {
      const providers = { deepseek: { type: 'deepseek', keys: [] } }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ providers }))

      const result = await configApi.addProvider('deepseek', { type: 'deepseek', api_key: 'sk-x' })

      expect(result).toEqual(providers)
      expect(apiClient.post).toHaveBeenCalledWith('/ext/llm_service/config/llm/providers', {
        provider_id: 'deepseek',
        config: { type: 'deepseek', api_key: 'sk-x' },
      })
    })

    it('deleteProvider DELETE 并解包 providers', async () => {
      const providers = {}
      vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({ providers }))

      const result = await configApi.deleteProvider('deepseek')

      expect(result).toEqual({})
      expect(apiClient.delete).toHaveBeenCalledWith(
        '/ext/llm_service/config/llm/providers/deepseek',
      )
    })
  })
})
