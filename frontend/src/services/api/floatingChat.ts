/**
 * 悬浮窗 API 服务
 *
 * 暴露接口：
 * - getFloatingChatStatus(): 获取悬浮窗应用状态
 * - launchFloatingChat(request): 启动悬浮窗应用
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface FloatingChatStatus {
  available: boolean
  executable_path?: string
  message: string
}

export interface LaunchRequest {
  session_id?: string
  token?: string
}

export interface LaunchResult {
  success: boolean
  message: string
}

export async function getFloatingChatStatus(): Promise<FloatingChatStatus> {
  const response = await apiClient.get<FloatingChatStatus>(API_ENDPOINTS.FLOATING_CHAT.STATUS)
  return response.data
}

export async function launchFloatingChat(request?: LaunchRequest): Promise<LaunchResult> {
  const response = await apiClient.post<LaunchResult>(API_ENDPOINTS.FLOATING_CHAT.LAUNCH, request)
  return response.data
}

export const floatingChatApi = {
  getFloatingChatStatus,
  launchFloatingChat,
}
