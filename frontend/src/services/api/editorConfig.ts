/**
 * 编辑器配置 API 服务
 *
 * 封装编辑器配置相关的 REST API 请求，
 * 包括获取/更新编辑器配置，以及根据文件路径解析编辑器类型。
 *
 * 暴露接口：
 * - getEditorConfig(): 获取编辑器配置
 * - updateEditorConfig(config): 更新编辑器配置
 * - resolveEditor(filePath): 根据文件路径解析编辑器类型
 */

import { apiClient } from './client'

const BASE = '/api/v1/config/editor'

/**
 * 获取编辑器配置
 *
 * 从后端获取编辑器映射、默认编辑器和编辑器定义。
 *
 * @returns 后端响应，包含 mappings、default_editor、editors 等字段
 */
export async function getEditorConfig(): Promise<any> {
  return apiClient.get(BASE)
}

/**
 * 更新编辑器配置
 *
 * @param config - 编辑器配置对象，包含 mappings、default_editor 等字段
 * @returns 后端响应
 */
export async function updateEditorConfig(config: Record<string, any>): Promise<any> {
  return apiClient.put(BASE, config)
}

/**
 * 根据文件路径解析编辑器类型
 *
 * 后端根据文件后缀名返回对应的编辑器类型（ide/builtin/external）。
 *
 * @param filePath - 文件路径，如 '/project/src/index.ts'
 * @returns 后端响应，包含 editor 字段表示编辑器类型
 */
export async function resolveEditor(filePath: string): Promise<any> {
  return apiClient.get(`${BASE}/resolve`, { params: { path: filePath } })
}
