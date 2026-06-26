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

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { mapThreadToSession, type ThreadStateResponse } from '@/utils/mappers'
import { requestWithRetry } from '@/utils/retry'
import type { Message, MessageToolCall, Session } from '@/types/models'
import type { MessagePart } from '@/types/messageParts'
import { checkIsSystemMessage } from '@/utils/messageType'
import type { RetryOptions } from '@/utils/retry'
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
  /** 线程标题（可选） */
  title?: string
  /** 用户意图（可选，兼容旧接口） */
  intent?: string
  /** 元数据（可选） */
  metadata?: Record<string, unknown>
  /** 绑定的 Agent ID（可选）- Requirements: 6.1 */
  agent_id?: string
}

/**
 * 后端线程创建响应类型
 */
interface ThreadCreateResponse {
  /** 线程ID */
  thread_id: string
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
  /** 关联的管道 ID 列表 */
  pipeline_ids?: string[]
  /** 当前活跃的管道 ID */
  active_pipeline_id?: string | null
}

/**
 * 后端消息响应类型
 */
interface BackendMessageResponse {
  id: string
  thread_id: string
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
  attachments?: Array<{
    id?: string
    name: string
    type?: string
    mime_type?: string
    url: string
    size?: number
  }>
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
  sessionId: string,
): Message {
  if (backendMessage.role === 'tool') {
    return {
      id: backendMessage.id,
      sessionId: sessionId,
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

  let toolCalls: MessageToolCall[] | undefined
  if (backendMessage.toolCalls && Array.isArray(backendMessage.toolCalls)) {
    // 后端 toolCalls[] 子项已统一为 camelCase（ToolCallItem 模型）。
    // 映射到前端 MessageToolCall（snake_case 内部表示）。
    toolCalls = backendMessage.toolCalls.map((tc) => ({
      call_id: (tc.callId || '') as string,
      tool_name: (tc.toolName || '') as string,
      tool_args: ((tc.toolArgs || {}) as Record<string, unknown>),
      status: (tc.status || 'completed') as 'pending' | 'running' | 'completed' | 'failed',
      result: tc.result,
      error: tc.error as string | undefined,
      duration_ms: tc.durationMs as number | undefined,
      containerTaskId: tc.containerTaskId as string | undefined,
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
    const thinkingStr = metadata.thinkingContent as string | undefined
    if (thinkingStr && typeof thinkingStr === 'string' && thinkingStr.length > 0) {
      thinking = {
        content: thinkingStr,
        isThinking: false,
      }
    }
  }

  const parts: MessagePart[] = []
  let seq = 0

  if (thinking?.content?.trim()) {
    parts.push({
      type: 'thinking',
      content: thinking.content,
      state: 'done',
      sequence: seq++,
    })
  }

  const isSystemMsg = checkIsSystemMessage(backendMessage.role, metadata)

  if (backendMessage.content?.trim()) {
    if (isSystemMsg) {
      parts.push({
        type: 'system',
        content: backendMessage.content,
        level: (metadata?.notification_level as any) || 'info',
        notificationType: (metadata?.notification_type as string) || 'task_notification',
        sequence: seq++,
      })
    } else {
      parts.push({
        type: 'text',
        content: backendMessage.content,
        state: 'done',
        sequence: seq++,
      })
    }
  }

  if (toolCalls && toolCalls.length > 0) {
    for (const tc of toolCalls) {
      parts.push({
        type: 'tool_call',
        callId: tc.call_id || '',
        name: tc.tool_name || '',
        args: tc.tool_args || {},
        state: 'done',
        result: tc.result,
        error: tc.error,
        sequence: seq++,
        // BUG-FIX-fix_20260606_file_opener_container_task_id:
        // 从后端 API 恢复 containerTaskId，确保历史消息加载后
        // 工具卡片的"打开文件"功能能正确解析工作空间路径。
        containerTaskId: tc.container_task_id || undefined,
      })
    }
  }

  // BUG-FIX-fix_20260528_system_msg_render:
  // 问题根因: 后端引擎将系统通知存储为 type='user'，API 返回 role='user'，
  //           导致前端合并后系统消息丢失系统样式
  // 修复方案: 当 isSystemMsg 为 true 时，将 role 修正为 'system'
  const effectiveRole = isSystemMsg ? 'system' : backendMessage.role as Message['role']

  return {
    id: backendMessage.id,
    sessionId: sessionId,
    sequence: backendMessage.sequence,
    role: effectiveRole,
    content: backendMessage.content,
    timestamp: backendMessage.timestamp,
    agentId: backendMessage.agentId,
    metadata: {
      ...backendMessage.metadata,
      ...(backendMessage.agentName ? { agentName: backendMessage.agentName } : {}),
    },
    clientMessageId: (backendMessage.metadata?.client_message_id as string | undefined) ?? undefined,
    attachments: backendMessage.attachments,
    thinking,
    parts: parts.length > 0 ? parts : undefined,
  }
}

/**
 * 消除合并组内 part.sequence 的冲突，保持每条消息内 parts 的逻辑顺序
 *
 * 渲染层（buildFragmentsFromParts）按 part.sequence 数值升序渲染，
 * 因此合并后 parts 的 sequence 数值顺序必须等于其逻辑顺序（thinking→text→tool）。
 *
 * 两条约束：
 * 1. 单条消息内 parts 已是正确逻辑顺序，合并后不能因「全局数值排序」打散——
 *    多条 API 消息各自 parts 从 0 起算，若按数值排序会把所有 thinking 聚到前面、
 *    所有 text 聚到后面，思考内容与所属回复「分家」（见复现测试）。
 * 2. 不破坏流式期间分配的大数 sequence（Date.now()），避免与未合并消息的 React key 碰撞。
 *
 * 策略：按「消息分组」处理，每组保留内部 parts 的原始相对顺序；
 *   - 组内 sequence 无冲突（流式大数，单条消息）→ 原样保留；
 *   - 组内/跨组出现冲突（多条 API 消息各自从 0 起算）→ 从组内最大 sequence +1 续接，
 *     续接发生在出现冲突的具体 part 上，不打乱其它 part 的既有顺序。
 *
 * BUG-FIX-fix_20260622_part_sequence_collision:
 *   保留流式大数 sequence，不全局重编。
 * BUG-FIX-fix_20260624_thinking_text_split:
 *   问题根因: 上一版修复用全局数值排序去重，把「A.thinking(0),B.thinking(0),A.text(1),
 *     B.text(1)」排成 thinking×2→text×2，思考与回复分家。改为按消息分组顺序处理，
 *     保持每条消息内 parts 的原始相对顺序。
 */
function dedupePartSequences(partsByMessage: any[][]): any[] {
  const result: any[] = []
  const seen = new Set<number>()
  // 组内最大 sequence：续接基准，随处理推进单调递增
  let maxSeq = 0
  for (const group of partsByMessage) {
    for (const p of group) {
      const seq = p.sequence
      if (seq != null && !seen.has(seq)) {
        // 无冲突：保留原 sequence，仅更新基准
        seen.add(seq)
        if (seq > maxSeq) maxSeq = seq
        result.push(p)
      } else {
        // 冲突或缺失：从当前最大 sequence +1 续接，保证单调且不与已有值碰撞
        maxSeq += 1
        while (seen.has(maxSeq)) maxSeq += 1
        seen.add(maxSeq)
        p.sequence = maxSeq
        result.push(p)
      }
    }
  }
  return result
}

/**
 * 合并连续的 assistant 消息 + 吸收夹在中间的 tool 消息（仅用于历史 API 加载）
 *
 * 后端将同一次 LLM 响应的 text 和 tool_calls 拆成多条 ExecutionRecordData，
 * 工具结果更是存成独立的 type=tool 记录。API 返回格式为:
 *   assistant(tool_call) | tool(result) | tool(result) | assistant(text)
 * 此函数将 tool 消息的结果注入前一个 assistant 的 tool_call part，
 * 再合并连续的 assistant 消息，保证与流式路径一致。
 *
 * BUG-FIX-fix_20260617_prepend_boundary_merge:
 * 导出此函数，供 pipelineMessageStore 在 prepend/initFromAPI 合并完整列表后调用，
 * 确保跨 API 分页边界的连续 assistant 消息也能被正确合并。
 *
 * BUG-FIX-fix_20260622_part_sequence_collision:
 * 问题根因: 原代码用全局计数器 globalSeq++ 重写所有 part 的 sequence，
 *   破坏了流式期间按 Date.now() 分配的唯一 sequence，导致合并后与未合并消息的
 *   part sequence 冲突，渲染层 React key 碰撞，出现「思考+文本+思考+文本」乱序
 *   和文本气泡重复。
 * 修复方案: 保留每个 part 的原始 sequence；仅当合并组内出现 sequence 冲突
 *   （多条 API 消息各自的 parts 都从 0 起算导致同值）时，对冲突 part 在组内
 *   从最大 sequence +1 续接。不破坏流式大数 sequence 的稳定性。
 * 影响范围: 切换页面/向上翻页时连续 assistant 消息的 part 渲染稳定性
 * 修复日期: 2026-06-22
 */
export function mergeConsecutiveAssistantMessages(messages: Message[]): Message[] {
  if (messages.length <= 1) return messages
  // 第一遍：将夹在 assistant 之间的 tool 消息的结果注入 tool_call part
  const absorbed: Message[] = []
  let i = 0
  while (i < messages.length) {
    const msg = messages[i]
    if (msg.role !== 'assistant') {
      absorbed.push(msg)
      i++
      continue
    }
    const assistant = { ...msg, parts: msg.parts ? [...msg.parts] : undefined }
    const toolParts = (assistant.parts || []).filter((p: any) => p.type === 'tool_call')
    i++
    while (i < messages.length && messages[i].role === 'tool') {
      const tm = messages[i]
      const tcId = tm.toolCallId
      if (tcId) {
        const target = toolParts.find((p: any) => p.callId === tcId)
        if (target) {
          target.result = tm.toolResult
          target.error = tm.toolError
          target.state = 'done' as const
          target.durationMs = tm.durationMs ?? target.durationMs
        }
      }
      i++
    }
    absorbed.push(assistant)
  }
  // 第二遍：合并连续的 assistant
  const result: Message[] = []
  let j = 0
  while (j < absorbed.length) {
    const msg = absorbed[j]
    if (msg.role !== 'assistant') {
      result.push(msg)
      j++
      continue
    }
    const groupStart = j
    while (j < absorbed.length && absorbed[j].role === 'assistant') { j++ }
    const group = absorbed.slice(groupStart, j)
    if (group.length === 1) {
      result.push(group[0])
      continue
    }
    const first = group[0]
    const mergedContent = group
      .map((m) => m.content)
      .filter((c) => c?.trim())
      .join('\n\n')
    // 保留每个 part 的原始 sequence（流式大数 / API 局部小数），仅消除组内冲突。
    // 按消息分组传入（而非 flatMap 打平），dedupePartSequences 据此保持每条消息内
    // parts 的逻辑顺序，避免思考内容与回复分家。
    const partsByMessage = group.map((m) => (m.parts || []).map((p: any) => ({ ...p })))
    const mergedParts = dedupePartSequences(partsByMessage)
    if (!mergedContent && mergedParts.length === 0) {
      for (const m of group) result.push(m)
      continue
    }
    result.push({
      ...first,
      content: mergedContent,
      parts: mergedParts.length > 0 ? mergedParts : undefined,
    } as Message)
  }
  return result
}

export async function getSessions(options: RetryOptions = {}): Promise<Session[]> {
  return requestWithRetry(async () => {
    // 只获取主管道会话（session_type=main_pipeline），过滤子任务管道
    const response = await apiClient.get<any>(API_ENDPOINTS.THREADS.LIST, {
      params: { session_type: 'main_pipeline', limit: 100 },
    })

    // BUG-FIX-fix_20260513_sessions_empty: 后端返回 {threads: [...], total: N} 格式，非纯数组
    // 问题根因: response.data 是对象而非数组，Array.isArray 判断为 false 导致 threads 为空
    // 修复方案: 优先取 response.data.threads，兼容旧版纯数组格式
    const threads = Array.isArray(response.data)
      ? response.data
      : (response.data?.threads || [])
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
  retryOptions: RetryOptions = {},
): Promise<Session> {
  return requestWithRetry(async () => {
    const requestData: ThreadCreateRequest = {}

    if (options.title !== undefined) {
      requestData.title = options.title
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
      },
    )

    // 将创建响应转换为ThreadStateResponse格式，然后映射为Session
    const threadState: ThreadStateResponse = {
      thread_id: response.data.thread_id,
      current_state: response.data.current_state || 'created',
      intent: response.data.intent || null,
      created_at: response.data.created_at,
      updated_at: response.data.updated_at || response.data.created_at,
      agent_id: response.data.agent_id || null,
      pipeline_ids: response.data.pipeline_ids || [],
      active_pipeline_id: response.data.active_pipeline_id || null,
    }

    return mapThreadToSession(threadState)
  }, retryOptions)
}

export async function deleteSession(sessionId: string, options: RetryOptions = {}): Promise<void> {
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
    pipelineRunId?: string
    depth?: number
    executorType?: 'agent' | 'tool' | 'user' | 'workflow'
    skip?: number
    limit?: number
    before_sequence?: number
  },
  options: RetryOptions = {},
): Promise<{ messages: Message[]; total: number; has_more: boolean }> {
  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    // 构建查询参数
    const params: Record<string, any> = {}
    if (filters) {
      if (filters.agentId) params.agent_id = filters.agentId
      if (filters.parentId) params.parent_id = filters.parentId
      if (filters.pipelineRunId) params.pipeline_run_id = filters.pipelineRunId
      if (filters.depth !== undefined) params.depth = filters.depth
      if (filters.executorType) params.executor_type = filters.executorType
      if (filters.skip !== undefined) params.skip = filters.skip
      if (filters.limit !== undefined) params.limit = filters.limit
      if (filters.before_sequence !== undefined) params.before_sequence = filters.before_sequence
      if (filters.after_sequence !== undefined) params.after_sequence = filters.after_sequence
    }

    const response = await apiClient.get<any>(API_ENDPOINTS.MESSAGES.LIST(sessionId), { params })

    // 后端 MessageListResponse 始终是对象格式 {messages, total, has_more}
    const rawMessages = response.data.messages || []
    const mapped = rawMessages.map((msg: BackendMessageResponse) =>
      mapBackendMessageToMessage(msg, sessionId),
    )
    const merged = mergeConsecutiveAssistantMessages(mapped)
    return {
      messages: merged,
      total: response.data.total ?? rawMessages.length,
      has_more: response.data.has_more ?? false,
    }
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
  options: RetryOptions = {},
): Promise<Session> {
  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    // PATCH 现在返回完整的 ThreadResponse，无需二次 GET
    const response = await apiClient.patch<ThreadStateResponse>(
      API_ENDPOINTS.THREADS.UPDATE_AGENT(sessionId),
      { agent_id: agentId },
    )

    return mapThreadToSession(response.data)
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
  /** 元数据（可选） */
  metadata?: Record<string, unknown>
}

export async function updateSession(
  sessionId: string,
  options: UpdateSessionOptions = {},
): Promise<Session> {
  const { title, agentId, metadata, ...retryOptions } = options

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
    if (metadata !== undefined) {
      requestData.metadata = metadata
    }

    const response = await apiClient.patch<ThreadUpdateResponse>(
      API_ENDPOINTS.THREADS.UPDATE(sessionId),
      requestData,
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
