/**
 * 工具管理 API 服务
 *
 * 提供工具的查询、生成、删除等接口
 * 与后端 /api/v1/tools/* 端点对齐
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
 * 工具生成请求类型
 */
export interface ToolGenerateRequest {
  /** 工具名称 */
  name: string
  /** 工具描述 */
  description: string
  /** 工具分类 */
  category?: string
  /** 参数定义 */
  parameters?: Record<string, unknown>
  /** 代码实现 */
  code?: string
}

/**
 * 工具更新请求类型
 */
export interface ToolUpdateRequest {
  /** 工具状态 */
  status?: 'active' | 'inactive'
  /** 工具描述 */
  description?: string
  /** 工具分类 */
  category?: string
  /** 参数定义 */
  parameters?: Record<string, unknown>
}

/**
 * 代码条目响应类型
 */
export interface CodeEntryResponse {
  /** 条目 ID */
  id: string
  /** 代码内容 */
  code: string
  /** 语言 */
  language: string
  /** 文件路径 */
  file_path?: string
  /** 行号范围 */
  line_range?: { start: number; end: number }
}

/**
 * 代码搜索结果类型
 */
export interface CodeSearchResult {
  /** 搜索结果列表 */
  items: CodeEntryResponse[]
  /** 总数量 */
  total: number
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

/**
 * 获取单个工具详情
 *
 * @param toolId 工具名称/ID
 * @param options 重试选项
 * @returns 工具详情
 */
export async function getTool(toolId: string, options: RetryOptions = {}): Promise<ToolResponse> {
  if (!toolId || toolId.trim().length === 0) {
    throw new Error('工具 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<ToolResponse>(API_ENDPOINTS.TOOLS.GET(toolId))
    return response.data
  }, options)
}

/**
 * 生成工具
 *
 * @param data 生成请求数据
 * @param options 重试选项
 * @returns 生成的工具
 */
export async function generateTool(
  data: ToolGenerateRequest,
  options: RetryOptions = {},
): Promise<ToolResponse> {
  if (!data.name || data.name.trim().length === 0) {
    throw new Error('工具名称不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<ToolResponse>(API_ENDPOINTS.TOOLS.GENERATE, data)
    return response.data
  }, options)
}

/**
 * 删除工具
 *
 * @param toolId 工具名称/ID
 * @param options 重试选项
 */
export async function deleteTool(toolId: string, options: RetryOptions = {}): Promise<void> {
  if (!toolId || toolId.trim().length === 0) {
    throw new Error('工具 ID 不能为空')
  }

  return requestWithRetry(async () => {
    await apiClient.delete(API_ENDPOINTS.TOOLS.DELETE(toolId))
  }, options)
}

/**
 * 更新工具
 *
 * @param toolId 工具名称/ID
 * @param data 更新数据
 * @param options 重试选项
 * @returns 更新后的工具
 */
export async function updateTool(
  toolId: string,
  data: ToolUpdateRequest,
  options: RetryOptions = {},
): Promise<ToolResponse> {
  if (!toolId || toolId.trim().length === 0) {
    throw new Error('工具 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.patch<ToolResponse>(API_ENDPOINTS.TOOLS.UPDATE(toolId), data)
    return response.data
  }, options)
}

/**
 * 获取代码条目
 *
 * @param entryId 条目 ID
 * @param options 重试选项
 * @returns 代码条目
 */
export async function getCodeEntry(
  entryId: string,
  options: RetryOptions = {},
): Promise<CodeEntryResponse> {
  if (!entryId || entryId.trim().length === 0) {
    throw new Error('条目 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<CodeEntryResponse>(API_ENDPOINTS.TOOLS.CODE(entryId))
    return response.data
  }, options)
}

/**
 * 搜索代码
 *
 * @param query 搜索关键词
 * @param options 重试选项
 * @returns 搜索结果
 */
export async function searchCode(
  query: string,
  options: RetryOptions = {},
): Promise<CodeSearchResult> {
  if (!query || query.trim().length === 0) {
    throw new Error('搜索关键词不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<CodeSearchResult>(API_ENDPOINTS.TOOLS.CODE_SEARCH, {
      params: { query },
    })
    return response.data
  }, options)
}

/**
 * 回滚工具版本
 *
 * @param toolId 工具名称/ID
 * @param version 目标版本号（可选，默认回滚到上一版本）
 * @param options 重试选项
 * @returns 回滚后的工具
 */
export async function rollbackTool(
  toolId: string,
  version?: number,
  options: RetryOptions = {},
): Promise<ToolResponse> {
  if (!toolId || toolId.trim().length === 0) {
    throw new Error('工具 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<ToolResponse>(API_ENDPOINTS.TOOLS.ROLLBACK(toolId), {
      version,
    })
    return response.data
  }, options)
}
