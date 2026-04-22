/**
 * 会话和消息 API 服务
 *
 * 提供 getSessions、createSession、deleteSession、getMessages 接口，内部调用后端 Thread API，并使用数据映射函数转换响应
 *
 * Requirements: 3.1, 3.2, 3.3, 3.4
 *
 * 暴露接口：
 * - getSessions(options): Session[] - 获取会话列表
 * - createSession(options, retryOptions): Session - 创建新会话
 * - deleteSession(sessionId, options): void - 删除会话
 * - getMessages(sessionId, filters, options): Message[] - 获取会话消息列表（支持嵌套结构筛选）
 * - updateSessionAgent(sessionId, agentId, options): Session - 更新会话绑定的 Agent
 * - updateSession(sessionId, options): Session - 更新会话（标题和/或 Agent）
 * - CreateSessionOptions - 创建会话选项
 * - UpdateSessionOptions - 更新会话选项
 */

import {
    API_ENDPOINTS,
} from '@/../constants/api'
import type { Message, Session } from '@/../types/models'
import {
    mapThreadToSession,
    type ThreadStateResponse,
} from '@/../utils/mappers'
import type { RetryOptions } from '@/../utils/retry'
import { requestWithRetry } from '@/../utils/retry'
import apiClient from '@/services/api/client'
// 注意：GetMessagesResponse已被BackendMessagesListResponse替代，用于直接映射后端响应

/**
 * 后端线程列表响应类型
 */
interface ThreadListResponse {
  /** 线程列表 */
  threads: ThreadStateResponse[]
  /** 总数 */
  total?: number
}

/**
 * 后端线程创建请求类型
 */
interface ThreadCreateRequest {
  /** 用户意图（可选） */
  intent?: string
  /** 元数据（可选） */
  metadata?: Record<string, unknown>
  /** 绑定的 Agent ID（可选）- Requirements: 6.1 */
  agent_id?: string
}

/**
 * 后端线程创建响应类型
 *
 * 注意：后端可能返回 session_id 或 thread_id，需要兼容两种格式
 */
interface ThreadCreateResponse {
  /** 线程ID（新格式） */
  session_id?: string
  /** 线程ID（旧格式） */
  thread_id?: string
  /** 创建时间 */
  created_at: string
  /** 当前状态 */
  current_state?: string
  /** 用户意图 */
  intent?: string | null
  /** 更新时间 */
  updated_at?: string
  /** 绑定的 Agent ID - Requirements: 6.3 */
  agent_id?: string | null
}

/**
 * 后端消息响应类型
 */
interface BackendMessageResponse {
  id: string
  thread_id: string
  parentId?: string
  sequence?: number
  role: string
  content: string
  timestamp: string
  status?: string
  agentId?: string
  agentName?: string
  metadata?: Record<string, unknown>
  toolCalls?: Array<Record<string, unknown>>
  toolCallId?: string
  toolName?: string
  toolArgs?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  durationMs?: number
}

/**
 * 后端消息列表响应类型
 */
interface BackendMessagesListResponse {
  /** 消息列表 */
  messages: BackendMessageResponse[]
  /** 总数 */
  total?: number
}

/**
 * 参数验证错误
 */
class ValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ValidationError'
  }
}

/**
 * 验证会话ID
 */
function validateSessionId(sessionId: string): void {
  if (!sessionId || sessionId.trim().length === 0) {
    throw new ValidationError('会话ID不能为空')
  }
}

function mapBackendMessageToMessage(
  backendMessage: BackendMessageResponse,
  sessionId: string
): Message {
  if (backendMessage.role === 'tool') {
    return {
      id: backendMessage.id,
      sessionId: sessionId,
      parentId: backendMessage.parentId,
      sequence: backendMessage.sequence ?? 0,
      role: 'tool',
      content: backendMessage.content,
      timestamp: backendMessage.timestamp,
      agentId: backendMessage.agentId,
      status: backendMessage.status || 'completed',
      toolCallId: backendMessage.toolCallId,
      toolName: backendMessage.toolName,
      toolArgs: backendMessage.toolArgs,
      toolResult: backendMessage.toolResult,
      toolError: backendMessage.toolError,
      durationMs: backendMessage.durationMs,
      metadata: backendMessage.metadata,
    } as Message
  }

  let toolCalls: Message['toolCalls']
  if (backendMessage.toolCalls && Array.isArray(backendMessage.toolCalls)) {
    toolCalls = backendMessage.toolCalls.map(tc => ({
      call_id: (tc.call_id || tc.callId || tc.tool_call_id || '') as string,
      tool_name: (tc.tool_name || tc.toolName || tc.name || '') as string,
      tool_args: (tc.tool_args || tc.toolArgs || tc.args || tc.parameters || {}) as Record<string, unknown>,
      status: (tc.status || 'completed') as 'pending' | 'running' | 'completed' | 'failed',
      result: tc.result,
      error: tc.error as string | undefined,
      duration_ms: (tc.duration_ms || tc.durationMs) as number | undefined,
    }))
  }

  // BUG-FIX-fix_20260406_thinking_missing: 从 metadata 中恢复思考内容
  // 问题根因: mapBackendMessageToMessage 缺少 thinking 字段提取逻辑，
  //           页面刷新后从 API 加载消息时，thinking 内容虽然在 metadata.thinking_content 中，
  //           但没有被转换为 message.thinking 字段，导致思考内容不显示
  // 修复方案: 与 toMessage 函数保持一致，从 metadata.thinking_content 提取并转换为 ThinkingContent 对象
  let thinking: Message['thinking'] = undefined
  const metadata = backendMessage.metadata
  if (metadata) {
    const thinkingStr = (metadata.thinkingContent || metadata.thinking_content) as string | undefined
    if (thinkingStr && typeof thinkingStr === 'string' && thinkingStr.length > 0) {
      thinking = {
        content: thinkingStr,
        isThinking: false,
      }
    }
  }

  return {
    id: backendMessage.id,
    sessionId: sessionId,
    parentId: backendMessage.parentId,
    sequence: backendMessage.sequence,
    role: backendMessage.role as Message['role'],
    content: backendMessage.content,
    timestamp: backendMessage.timestamp,
    agentId: backendMessage.agentId,
    metadata: {
      ...backendMessage.metadata,
      ...(backendMessage.agentName ? { agentName: backendMessage.agentName } : {}),
    },
    toolCalls,
    thinking,
  }
}

export async function getSessions(
  options: RetryOptions = {}
): Promise<Session[]> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<ThreadListResponse>(
      API_ENDPOINTS.THREADS.LIST
    )

    // 使用数据映射函数将Thread转换为Session
    const threads = response.data.threads || []
    return threads.map(mapThreadToSession)
  }, options)
}

/**
 * 创建会话选项
 */
export interface CreateSessionOptions {
  /** 会话标题（可选） */
  title?: string
  /** 绑定的 Agent ID（可选） */
  agentId?: string
}

export async function createSession(
  options: CreateSessionOptions = {},
  retryOptions: RetryOptions = {}
): Promise<Session> {
  return requestWithRetry(async () => {
    const requestData: ThreadCreateRequest = {}

    if (options.title !== undefined) {
      requestData.intent = options.title
    }

    if (options.agentId !== undefined) {
      requestData.agent_id = options.agentId
    }

    const response = await apiClient.post<ThreadCreateResponse>(
      API_ENDPOINTS.THREADS.CREATE,
      requestData,
      {
        headers: {
          'X-Main-Agent-Request': 'true',
        },
      }
    )

    // 将创建响应转换为ThreadStateResponse格式，然后映射为Session
    const threadState: ThreadStateResponse = {
      thread_id: response.data.session_id || response.data.thread_id || '',
      current_state: response.data.current_state || 'created',
      intent: response.data.intent || null,
      created_at: response.data.created_at,
      updated_at: response.data.updated_at || response.data.created_at,
      agent_id: response.data.agent_id || null,
    }

    return mapThreadToSession(threadState)
  }, retryOptions)
}



export async function deleteSession(
  sessionId: string,
  options: RetryOptions = {}
): Promise<void> {
  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    await apiClient.delete(API_ENDPOINTS.THREADS.DELETE(sessionId))
  }, options)
}

export async function getMessages(
  sessionId: string,
  filters?: {
    agentId?: string
    parentId?: string
    depth?: number
    executorType?: 'agent' | 'tool' | 'user' | 'workflow'
    skip?: number
    limit?: number
  },
  options: RetryOptions = {}
): Promise<Message[]> {
  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    // 构建查询参数
    const params: Record<string, any> = {}
    if (filters) {
      if (filters.agentId) params.agent_id = filters.agentId
      if (filters.parentId) params.parent_id = filters.parentId
      if (filters.depth !== undefined) params.depth = filters.depth
      if (filters.executorType) params.executor_type = filters.executorType
      if (filters.skip !== undefined) params.skip = filters.skip
      if (filters.limit !== undefined) params.limit = filters.limit
    }

    const response = await apiClient.get<BackendMessagesListResponse>(
      API_ENDPOINTS.MESSAGES.LIST(sessionId),
      { params }
    )

    // 将后端消息响应映射为前端消息模型
    const messages = response.data.messages || []
    return messages.map(msg => mapBackendMessageToMessage(msg, sessionId))
  }, options)
}

/**
 * 后端线程更新请求类型
 */
interface ThreadUpdateRequest {
  /** 用户意图/标题（可选） */
  intent?: string
  /** 绑定的 Agent ID（可选）- Requirements: 6.2 */
  agent_id?: string | null
  /** 元数据（可选） */
  metadata?: Record<string, unknown>
}

/**
 * 后端线程更新响应类型
 */
interface ThreadUpdateResponse {
  /** 线程ID */
  thread_id: string
  /** 当前状态 */
  current_state: string
  /** 用户意图 */
  intent: string | null
  /** 创建时间 */
  created_at: string
  /** 更新时间 */
  updated_at: string
  /** 绑定的 Agent ID - Requirements: 6.3 */
  agent_id?: string | null
}

export async function updateSessionAgent(
  sessionId: string,
  agentId: string | null,
  options: RetryOptions = {}
): Promise<Session> {
  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    const response = await apiClient.put<{
      thread_id: string
      agent_id: string | null
      updated_at: string
    }>(API_ENDPOINTS.THREADS.UPDATE_AGENT(sessionId), { agent_id: agentId })

    // 获取更新后的会话详情
    const detailResponse = await apiClient.get<ThreadUpdateResponse>(
      API_ENDPOINTS.THREADS.GET(sessionId)
    )

    // 将更新响应转换为ThreadStateResponse格式，然后映射为Session
    const threadState: ThreadStateResponse = {
      thread_id: detailResponse.data.thread_id || sessionId,
      current_state: detailResponse.data.current_state,
      intent: detailResponse.data.intent,
      created_at: detailResponse.data.created_at,
      updated_at: detailResponse.data.updated_at,
      agent_id: response.data.agent_id || null,
    }

    return mapThreadToSession(threadState)
  }, options)
}

/**
 * 更新会话选项
 */
interface UpdateSessionOptions extends RetryOptions {
  /** 会话标题（可选） */
  title?: string
  /** Agent ID（可选） */
  agentId?: string | null
}

export async function updateSession(
  sessionId: string,
  options: UpdateSessionOptions = {}
): Promise<Session> {
  const { title, agentId, ...retryOptions } = options

  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    // 构造更新请求
    const requestData: ThreadUpdateRequest = {}
    if (title !== undefined) {
      requestData.intent = title
    }
    if (agentId !== undefined) {
      requestData.agent_id = agentId
    }

    const response = await apiClient.put<ThreadUpdateResponse>(
      API_ENDPOINTS.THREADS.UPDATE(sessionId),
      requestData
    )

    // 将更新响应转换为ThreadStateResponse格式，然后映射为Session
    const threadState: ThreadStateResponse = {
      thread_id: response.data.thread_id || sessionId,
      current_state: response.data.current_state,
      intent: response.data.intent,
      created_at: response.data.created_at,
      updated_at: response.data.updated_at,
      agent_id: response.data.agent_id || null,
    }

    return mapThreadToSession(threadState)
  }, retryOptions)
}
