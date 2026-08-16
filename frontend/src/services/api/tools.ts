/**
 * 工具管理 API 服务
 *
 * 提供工具列表查询接口，与后端 GET /api/v1/tools 端点对齐。
 *
 * 2026-08 清理：原 getTool/generateTool/deleteTool/updateTool/getCodeEntry/
 * searchCode/rollbackTool 指向后端不存在的 /api/v1/tools/{id} 系列 CRUD 端点
 * （kernel server.rs 仅注册 GET /api/v1/tools 列表），已连同其用例删除。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { ToolCategory, ToolExample, ToolSource, ToolStatus } from '@/types/tool'
import type { RetryOptions } from '@/utils/retry'

/**
 * 工具响应类型（与后端 Tool 类对齐）
 */
export interface ToolResponse {
  /** 数据库 ID */
  id?: string
  /** 工具名称/ID */
  name: string
  /** 工具描述 */
  description: string

  /** 适用场景列表 */
  when_to_use?: string[]
  /** 不适用场景列表 */
  when_not_to_use?: string[]
  /** 使用示例列表 */
  examples?: ToolExample[]
  /** 注意事项列表 */
  caveats?: string[]

  /** 输入参数 Schema */
  input_schema?: Record<string, unknown>
  /** 输出 Schema */
  output_schema?: Record<string, unknown>

  /** 工具来源 */
  source: ToolSource
  /** 工具分类 */
  category?: ToolCategory
  /** 工具级别 */
  level?: string
  /** 版本号 */
  version?: string
  /** 标签 */
  tags?: string[]

  /** 工具状态 */
  status: ToolStatus
  /** 是否需要审批 */
  requires_approval?: boolean

  /** 创建时间 */
  created_at?: string
  /** 更新时间 */
  updated_at?: string
}

/**
 * 工具列表响应类型
 */
export interface ToolListResponse {
  /** 工具列表 */
  items: ToolResponse[]
  /** 总数量 */
  total: number
  /** 当前页码 */
  page: number
  /** 每页数量 */
  page_size: number
}

/**
 * 获取工具列表查询参数
 */
export interface GetToolsParams {
  /** 页码 */
  page?: number
  /** 每页数量 */
  pageSize?: number
  /** 分类过滤 */
  category?: string
  /** 来源过滤 */
  source?: string
  /** 状态过滤 */
  status?: string
  /** 搜索关键词 */
  search?: string
}

/**
 * 获取工具列表
 *
 * @param params 查询参数
 * @param options 重试选项
 * @returns 工具列表响应
 */
export async function getTools(
  params: GetToolsParams = {},
  options: RetryOptions = {},
): Promise<ToolListResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<ToolListResponse>(API_ENDPOINTS.TOOLS.LIST, {
      params: {
        page: params.page || 1,
        page_size: params.pageSize || 20,
        category: params.category,
        source: params.source,
        status: params.status,
        search: params.search,
      },
    })
    return response.data
  }, options)
}
