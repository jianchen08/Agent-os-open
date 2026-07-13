/**
 * 思考模式 API 服务
 *
 * 暴露接口：
 * - getThinkingModels(): 获取支持思考模式的模型列表
 * - getThinkingModeInfo(modelName): 获取指定模型的思考模式信息
 * - switchThinkingMode(currentModel, enableThinking): 切换思考模式
 * - getThinkingModeRecommendations(): 获取思考模式推荐
 * - checkThinkingModeSupport(modelName): 检查模型是否支持思考模式
 * - checkThinkingModeHealth(): 思考模式服务健康检查
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface ThinkingModeInfo {
  model_name: string
  thinking_type: string
  display_name: string
  base_model: string
  thinking_model: string
  is_same_model: boolean
  switch_description: string
  thinking_params: Record<string, unknown>
  normal_params: Record<string, unknown>
}

export interface ThinkingModelInfo {
  model_name: string
  display_name: string
  thinking_type: string
  base_model: string
  thinking_model: string
  is_same_model: boolean
  supports_reasoning_effort: boolean
  description: string
}

export interface ThinkingModeSwitchResponse {
  target_model: string
  params: Record<string, unknown>
  switch_type: string
  description: string
}

export interface ThinkingModeRecommendation {
  model_name: string
  display_name: string
  thinking_type: string
  suitability_score: number
  optimal_params: Record<string, unknown>
  best_for: string[]
  tips: string[]
  cost_estimate: string
}

export async function getThinkingModels(): Promise<ThinkingModelInfo[]> {
  const response = await apiClient.get<ThinkingModelInfo[]>(API_ENDPOINTS.THINKING_MODE.MODELS)
  return response.data
}

export async function getThinkingModeInfo(modelName: string): Promise<ThinkingModeInfo> {
  const response = await apiClient.get<ThinkingModeInfo>(
    API_ENDPOINTS.THINKING_MODE.MODEL_INFO(modelName),
  )
  return response.data
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

export async function getThinkingModeRecommendations(
  taskType: string = 'general',
  complexity: string = 'medium',
): Promise<ThinkingModeRecommendation[]> {
  const response = await apiClient.post<ThinkingModeRecommendation[]>(
    API_ENDPOINTS.THINKING_MODE.RECOMMENDATIONS,
    {
      task_type: taskType,
      complexity: complexity,
    },
  )
  return response.data
}

export async function checkThinkingModeSupport(modelName: string): Promise<{
  model_name: string
  supports_thinking: boolean
  thinking_type?: string
  display_name?: string
  switch_description?: string
}> {
  const response = await apiClient.get(API_ENDPOINTS.THINKING_MODE.CHECK_SUPPORT(modelName))
  return response.data
}

export async function checkThinkingModeHealth(): Promise<{
  status: string
  available_models: number
  service: string
}> {
  const response = await apiClient.get(API_ENDPOINTS.THINKING_MODE.HEALTH)
  return response.data
}
