/**
 * 模块 API 服务
 *
 * 提供模块 UI Schema 的获取接口
 */
import apiClient from './client'

/**
 * 获取所有模块的 UI Schema
 */
export async function getModuleUISchemas() {
  const response = await apiClient.get('/ext/channel_api/modules/ui')
  return response.data
}
