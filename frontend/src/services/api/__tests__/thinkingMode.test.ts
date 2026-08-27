/**
 * 思考模式 API 服务测试
 *
 * 覆盖 /ext/llm_service/thinking-mode/switch 端点封装：切换思考模式。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as thinkingModeApi from '@/services/api/thinkingMode'

vi.mock('../client', () => ({
  default: {
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
})
