/**
 * 动态数据源 API 服务
 *
 * 对接内核代理端点 GET /api/v1/datasource/{uri}，获取动态选项列表。
 * 典型场景：插件 ui.input_form 中 select 字段的 options 从后端动态获取。
 *
 * @module datasource
 */

import apiClient from './client'
import type { DynamicDataSourceResponse } from '@/types/schema'

/**
 * 获取动态数据源
 *
 * 调用内核代理端点获取选项列表。
 *
 * @param uri - 数据源 URI（如 "categories/list"）
 * @param params - 可选请求参数
 * @returns 数据源响应
 * @throws 网络错误时抛出异常
 */
export async function fetchDynamicDataSource(
  uri: string,
  params?: Record<string, unknown>,
): Promise<DynamicDataSourceResponse> {
  const response = params
    ? await apiClient.get(`/api/v1/datasource/${uri}`, { params })
    : await apiClient.get(`/api/v1/datasource/${uri}`)
  return response.data
}
