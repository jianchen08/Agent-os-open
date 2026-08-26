/**
 * 思考模式 API 服务测试
 *
 * 覆盖 /ext/llm_service/thinking-mode* 端点封装：模型列表、模型信息、
 * 切换、推荐、支持检查、健康检查。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as thinkingModeApi from '@/services/api/thinkingMode'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

describe('思考模式 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getThinkingModels - 模型列表', () => {
    it('请求 models 端点并解包', async () => {
      const models = [
        {
          model_name: 'deepseek-r1',
          display_name: 'DeepSeek R1',
          thinking_type: 'reasoning',
          base_model: 'deepseek-chat',
          thinking_model: 'deepseek-r1',
          is_same_model: false,
          supports_reasoning_effort: true,
          description: 'x',
        },
      ]
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(models))

      const result = await thinkingModeApi.getThinkingModels()

      expect(result).toHaveLength(1)
      expect(result[0].supports_reasoning_effort).toBe(true)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/llm_service/thinking-mode/models')
    })
  })

  describe('getThinkingModeInfo - 模型信息', () => {
    it('按模型名请求并解包', async () => {
      const info = {
        model_name: 'deepseek-r1',
        thinking_type: 'reasoning',
        display_name: 'DeepSeek R1',
        base_model: 'deepseek-chat',
        thinking_model: 'deepseek-r1',
        is_same_model: false,
        switch_description: 'd',
        thinking_params: {},
        normal_params: {},
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(info))

      const result = await thinkingModeApi.getThinkingModeInfo('deepseek-r1')

      expect(result.is_same_model).toBe(false)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/llm_service/thinking-mode/models/deepseek-r1',
      )
    })
  })

  describe('switchThinkingMode - 切换', () => {
    it('POST 切换载荷', async () => {
      const resp = {
        target_model: 'deepseek-r1',
        params: {},
        switch_type: 'model_switch',
        description: 'd',
      }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(resp))

      const result = await thinkingModeApi.switchThinkingMode('deepseek-chat', true)

      expect(result.switch_type).toBe('model_switch')
      expect(apiClient.post).toHaveBeenCalledWith('/ext/llm_service/thinking-mode/switch', {
        current_model: 'deepseek-chat',
        enable_thinking: true,
      })
    })

    it('关闭思考模式时 enable_thinking=false', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({}))

      await thinkingModeApi.switchThinkingMode('deepseek-r1', false)

      expect(apiClient.post).toHaveBeenCalledWith('/ext/llm_service/thinking-mode/switch', {
        current_model: 'deepseek-r1',
        enable_thinking: false,
      })
    })
  })

  describe('getThinkingModeRecommendations - 推荐', () => {
    it('默认参数 general/medium', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse([]))

      const result = await thinkingModeApi.getThinkingModeRecommendations()

      expect(result).toEqual([])
      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/llm_service/thinking-mode/recommendations',
        { task_type: 'general', complexity: 'medium' },
      )
    })

    it('自定义任务类型与复杂度', async () => {
      const recs = [
        {
          model_name: 'deepseek-r1',
          display_name: 'DeepSeek R1',
          thinking_type: 'reasoning',
          suitability_score: 0.9,
          optimal_params: {},
          best_for: ['code'],
          tips: [],
          cost_estimate: 'low',
        },
      ]
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(recs))

      const result = await thinkingModeApi.getThinkingModeRecommendations('code', 'high')

      expect(result[0].suitability_score).toBe(0.9)
      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/llm_service/thinking-mode/recommendations',
        { task_type: 'code', complexity: 'high' },
      )
    })
  })

  describe('checkThinkingModeSupport - 支持检查', () => {
    it('请求 check 端点并解包', async () => {
      const resp = { model_name: 'deepseek-r1', supports_thinking: true }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await thinkingModeApi.checkThinkingModeSupport('deepseek-r1')

      expect(result.supports_thinking).toBe(true)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/llm_service/thinking-mode/check/deepseek-r1',
      )
    })
  })

  describe('checkThinkingModeHealth - 健康检查', () => {
    it('请求 healthz 端点并解包', async () => {
      const resp = { status: 'ok', available_models: 3, service: 'llm_service' }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await thinkingModeApi.checkThinkingModeHealth()

      expect(result.status).toBe('ok')
      expect(result.available_models).toBe(3)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/llm_service/thinking-mode/healthz')
    })
  })
})
