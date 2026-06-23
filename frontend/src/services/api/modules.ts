/**
 * 模块 API 服务
 *
 * 提供模块 UI Schema 的获取和客户端能力注册接口
 */
import apiClient from './client'

/**
 * 获取所有模块的 UI Schema
 */
export async function getModuleUISchemas() {
  const response = await apiClient.get('/api/v1/modules/ui')
  return response.data
}

/**
 * 注册客户端能力声明
 */
export async function registerClientCapabilities(capabilities: {
  renderingSpaces: string[]
  supportedWidgets: string[]
  clientType: string
  version: string
}) {
  const response = await apiClient.post('/api/v1/client/register', capabilities)
  return response.data
}
