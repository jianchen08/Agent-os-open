/**
 * 编辑器配置 API 服务
 *
 * 封装编辑器配置相关的 REST API 请求，
 * 以及根据文件路径解析编辑器类型。
 *
 * 暴露接口：
 * - resolveEditor(filePath): 根据文件路径解析编辑器类型
 *
 * 2026-08 清理：getEditorConfig/updateEditorConfig 已删除——指向后端无对应
 * 路由的 /ext/channel_api/config/editor（死代码，零消费方）。
 */

import { apiClient } from './client'

const BASE = '/ext/channel_api/config/editor'

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
