/**
 * 用户设置 API 服务
 *
 * 提供用户个人设置的获取和更新接口，与后端 /api/v1/users/settings 端点对齐
 *
 * 暴露接口：
 * - getUserSettings(options): UserSettingsResponse - 获取用户设置
 * - updateUserSettings(data, options): UserSettingsResponse - 更新用户设置
 */

import apiClient from '@/services/api/client'
import {
  API_ENDPOINTS,
} from '@/../constants/api'
import { requestWithRetry } from '@/../utils/retry'
import type { RetryOptions } from '@/../utils/retry'
import type {
  UserSettingsResponse,
  UserSettingsUpdateRequest,
} from '@/../types/api'

export async function getUserSettings(
  options: RetryOptions = {}
): Promise<UserSettingsResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<UserSettingsResponse>(
      API_ENDPOINTS.USER_SETTINGS.GET
    )
    return response.data
  }, options)
}

export async function updateUserSettings(
  data: UserSettingsUpdateRequest,
  options: RetryOptions = {}
): Promise<UserSettingsResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<UserSettingsResponse>(
      API_ENDPOINTS.USER_SETTINGS.UPDATE,
      data
    )
    return response.data
  }, options)
}
