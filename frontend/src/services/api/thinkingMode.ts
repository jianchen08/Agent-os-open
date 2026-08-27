/**
 * 思考模式 API 服务
 *
 * 暴露接口：
 * - switchThinkingMode(currentModel, enableThinking): 切换思考模式
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface ThinkingModeSwitchResponse {
  target_model: string
  params: Record<string, unknown>
  switch_type: string
  description: string
}

export async function switchThinkingMode(
  currentModel: string,
  enableThinking: boolean,
): Promise<ThinkingModeSwitchResponse> {
  const response = await apiClient.post<ThinkingModeSwitchResponse>(
    API_ENDPOINTS.THINKING_MODE.SWITCH,
    {
      current_model: currentModel,
      enable_thinking: enableThinking,
    },
  )
  return response.data
}
