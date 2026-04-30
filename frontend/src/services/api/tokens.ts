/**
 * Token 计算 API 服务
 *
 * 提供基于后端 tiktoken 的精确 Token 计算功能
 * 与后端 /api/v1/tokens/* 端点对齐
 */

import apiClient from '@/services/api/client'

/**
 * Token 计算请求类型
 */
export interface TokenCountRequest {
  /** 要计算的文本 */
  text: string
  /** 模型名称，用于选择编码器 */
  model?: string
}

/**
 * Token 计算响应类型
 */
export interface TokenCountResponse {
  /** Token 数量 */
  token_count: number
  /** 文本字符数 */
  text_length: number
  /** 使用的模型 */
  model: string
}

/**
 * 批量 Token 计算请求类型
 */
export interface BatchTokenCountRequest {
  /** 要计算的文本列表 */
  texts: string[]
  /** 模型名称 */
  model?: string
}

/**
 * 批量 Token 计算响应类型
 */
export interface BatchTokenCountResponse {
  /** Token 数量列表 */
  token_counts: number[]
  /** 总 Token 数 */
  total_tokens: number
  /** 使用的模型 */
  model: string
}

/**
 * 消息 Token 计算请求类型
 */
export interface MessageTokenCountRequest {
  /** 消息角色 */
  role: string
  /** 消息内容 */
  content: string
}

/**
 * 多条消息 Token 计算请求类型
 */
export interface MessagesTokenCountRequest {
  /** 消息列表 */
  messages: MessageTokenCountRequest[]
  /** 模型名称 */
  model?: string
}

/**
 * 消息 Token 计算响应类型
 */
export interface MessagesTokenCountResponse {
  /** Token 数量 */
  token_count: number
  /** 消息数量 */
  message_count: number
  /** 使用的模型 */
  model: string
}

/**
 * 上下文Token统计明细
 */
export interface ContextTokensBreakdown {
  /** 用户意图Token数 */
  user_intent: number
  /** Agent定义Token数 */
  agent_definition: number
  /** 领域知识Token数 */
  domain_knowledge: number
  /** 工具描述Token数（包括工具描述本身） */
  tool_descriptions: number
  /** 执行历史Token数 */
  execution_history: number
  /** 用户偏好Token数 */
  user_preferences: number
  /** 错误上下文Token数 */
  error_context: number
}

/**
 * 上下文Token统计响应类型
 */
export interface ContextTokensResponse {
  /** 总Token数 */
  total_tokens: number
  /** 明细 */
  breakdown: ContextTokensBreakdown
  /** 使用的模型名称 */
  model_name: string
  /** 模型上下文窗口大小 */
  context_window: number
}

/**
 * 计算文本的 Token 数量
 *
 * @param request - Token 计算请求
 * @returns Token 计算响应
 */
export async function countTokens(request: TokenCountRequest): Promise<TokenCountResponse> {
  const response = await apiClient.post<TokenCountResponse>('/api/v1/tokens/count', request)
  return response.data
}

/**
 * 批量计算多个文本的 Token 数量
 *
 * @param request - 批量 Token 计算请求
 * @returns 批量 Token 计算响应
 */
export async function countTokensBatch(
  request: BatchTokenCountRequest,
): Promise<BatchTokenCountResponse> {
  const response = await apiClient.post<BatchTokenCountResponse>(
    '/api/v1/tokens/count/batch',
    request,
  )
  return response.data
}

/**
 * 计算多条消息的总 Token 数量
 *
 * @param request - 消息 Token 计算请求
 * @returns 消息 Token 计算响应
 */
export async function countMessagesTokens(
  request: MessagesTokenCountRequest,
): Promise<MessagesTokenCountResponse> {
  const response = await apiClient.post<MessagesTokenCountResponse>(
    '/api/v1/tokens/count/messages',
    request,
  )
  return response.data
}

/**
 * 获取线程的上下文Token统计
 *
 * 根据上下文引擎构建的上下文进行Token统计，包括：
 * - 用户意图
 * - Agent定义
 * - 领域知识
 * - 工具描述（包括工具描述本身）
 * - 执行历史
 * - 用户偏好
 * - 错误上下文
 *
 * @param threadId - 线程ID
 * @returns 上下文Token统计响应
 */
export async function getContextTokens(threadId: string): Promise<ContextTokensResponse> {
  const response = await apiClient.get<ContextTokensResponse>(
    `/api/v1/threads/${threadId}/context-tokens`,
  )
  return response.data
}
