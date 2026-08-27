/** 会话和消息 API 服务 提供 getSessions、createSession、deleteSession、getMessages 接口，内部调用后端 Thread API，并使用数据映射函数转换响应 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { mapThreadToSession, type ThreadStateResponse } from '@/utils/mappers'
import { requestWithRetry } from '@/utils/retry'
import type { Message, MessageToolCall, Session } from '@/types/models'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'
import { checkIsSystemMessage } from '@/utils/messageType'
import type { RetryOptions } from '@/utils/retry'

/** 线程创建表单字段（插件 contributes.thread_fields 聚合声明） */
export interface ThreadField {
  /** 字段名（提交参数名） */
  name: string
  /** 字段类型：string/textarea/number/select/multiselect */
  type: string
  label?: string
  required?: boolean
  options?: Array<{ label: string; value: string }>
  description?: string
  /**
   * 值写入 thread metadata 的存储键（缺省 = name）。声明驱动：字段可来自
   * 不同插件，存储位置由各自表达（存量约定 workspace_mode/isolation_mode
   * 为 snake_case，与内核注入段的读取键一致）。
   */
  x_metadata_key?: string
  /**
   * 消息级 execution_context 的生效路径（'.' 分隔，如 workspace.mode）；
   * 未声明的字段不进 execution_context 协议面。
   */
  x_execution_path?: string
  /**
   * 值守卫（插件以表达约束自己的字段依赖）：
   * requires 指向的另一声明字段为空时，本字段值回退为 on_empty
   * （如工作空间拓扑 worktree 依赖工作空间目录已填写，空则 plain）。
   */
  x_guard?: { requires: string; on_empty: string }
}

/** 获取线程创建表单字段 schema（内置 + 插件贡献聚合） */
export async function getThreadSchema(
  retryOptions: RetryOptions = {},
): Promise<ThreadField[]> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<{ fields: ThreadField[] }>(
      API_ENDPOINTS.SESSIONS.SCHEMA,
    )
    return Array.isArray(response.data?.fields) ? response.data.fields : []
  }, retryOptions)
}

interface ThreadCreateRequest {
  title?: string
  /** 用户意图（兼容旧接口） */
  intent?: string
  metadata?: Record<string, unknown>
  agent_id?: string
  /** 会话工作空间绝对路径（项目目录） */
  workspace?: string
  /** 会话隔离模式：isolated（容器）/ non_isolated（宿主+审批） */
  isolation_mode?: 'isolated' | 'non_isolated'
}

interface ThreadCreateResponse {
  thread_id: string
  created_at: string
  current_state?: string
  intent?: string | null
  updated_at?: string
  agent_id?: string | null
  pipeline_ids?: string[]
  active_pipeline_id?: string | null
}

/**
 * 存活中间态条目（ADR 2026-08-27 §2.6 前端刷新恢复）：消息读取接口带出
 * 寄存器中未落库的流式中间态快照。键 = `<type>:<message_id>`（如
 * `chunk:mc_xxx`）；接口返回它 = 该消息尚未落库（流式进行中）。
 * value 形状对齐内核 chunk 累积快照（transient.rs accumulate_chunk）：
 * text_len/reasoning_len 为累计长度，blocks 为节流粒度摘要（当前只含
 * text 块；reasoning 内容不携带，仅长度）。
 */
export interface TransientStateEntry {
  key: string
  value: {
    text_len?: number
    reasoning_len?: number
    blocks?: Array<{ type?: string; content?: string }>
  }
}

/** 后端消息响应类型（DB 加载与 new_message 流式事件共用——冷热路径同构的输入契约） */
export interface BackendMessageResponse {
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
  reasoningContent?: string
  toolName?: string
  toolArgs?: Record<string, unknown>
  toolResult?: unknown
  /** 工具结果 envelope 的结构化数据（后端 tool_result_json.data 投影）。
   *  与流式 tool_result 事件的 result_data 同源——冷热路径数据结构一致的关键字段，
   *  工具卡片刷新后仍能渲染 +/- 徽标与结构化 diff。 */
  toolResultData?: unknown
  /** 工具执行耗时（后端 tool_result_json.duration_ms 投影）。 */
  toolDurationMs?: number
  /** 工具执行所在容器任务 ID（envelope metadata.container_task_id 投影）。 */
  containerTaskId?: string
  toolError?: string
  /** 工具执行失败错误信息（后端新字段：CRITICAL CONTEXT 中 tool-role 消息携带的 error）。
   *  与 toolError 同义；映射时优先取 error，兼容旧 toolError 兜底。 */
  error?: string
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

class ValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ValidationError'
  }
}

function validateSessionId(sessionId: string): void {
  if (!sessionId || sessionId.trim().length === 0) {
    throw new ValidationError('会话ID不能为空')
  }
}

/**
 * 后端消息 → 前端 Message 共享映射。
 *
 * 冷热路径同构的唯一入口：DB 历史加载（getMessages）与 new_message 流式事件
 * （messageHandler）都经它生成 parts[]——流式收到的消息与刷新后从数据库加载
 * 的数据形态一致，不再有两套 parts 构造逻辑。
 */
/** 工具结果消息（role=tool）映射：结果/结构化 envelope/错误/耗时字段投影 */
function buildToolResultMessage(
  backendMessage: BackendMessageResponse,
  sessionId: string,
): Message {
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
    // tool 消息的结果：后端把执行结果放在 content 里（分层持久化投影），
    // 旧路径放在 toolResult 字段。这里取 toolResult，为空则用 content 兜底，
    // 保证 merge 函数能把结果注入 assistant 的 tool_call part（ActivityCard 显示）。
    toolResult: backendMessage.toolResult ?? backendMessage.content,
    // 结构化结果 envelope（tool_result_json 投影）：与流式 result_data 同源，
    // merge 时注入 tool_call part 的 resultData。null 归一为 undefined
    // （对齐流式 handler 的 ?? 语义，失败工具双侧均为 undefined）。
    toolResultData: backendMessage.toolResultData ?? undefined,
    // 后端新字段为 error（tool-role 消息携带 status + error），旧字段为 toolError。
    // 优先取 error、兼容兜底，mergeConsecutiveAssistantMessages 据 toolError 判定失败。
    toolError: backendMessage.error ?? backendMessage.toolError,
    durationMs: backendMessage.toolDurationMs ?? backendMessage.durationMs,
    containerTaskId: backendMessage.containerTaskId,
    metadata: backendMessage.metadata,
  } as Message
}

/** 持久化 toolCalls 子项归一：兼容 ToolCallItem 与 OpenAI 双格式 → 前端 MessageToolCall */
function normalizePersistedToolCalls(
  rawList: Array<Record<string, unknown>>,
): MessageToolCall[] {
  return rawList.map((tc: any) => {
    const fn = tc.function || {}
    // OpenAI 规范的 arguments 是 JSON 字符串（持久化 rebuild 强制转字符串），
    // 解析回对象——与流式路径的 args（事件 payload 对象）结构一致。
    // 解析失败（截断/损坏数据）保留原值降级，不中断整批消息映射。
    let toolArgs = tc.toolArgs || fn.arguments || {}
    if (typeof toolArgs === 'string') {
      try {
        toolArgs = JSON.parse(toolArgs)
      } catch {
        // 非法 JSON：降级保留字符串
      }
    }
    return {
      call_id: (tc.callId || tc.id || '') as string,
      tool_name: (tc.toolName || fn.name || '') as string,
      tool_args: toolArgs as Record<string, unknown>,
      status: (tc.status || 'completed') as 'pending' | 'running' | 'completed' | 'failed',
      result: tc.result,
      resultData: tc.resultData,
      error: tc.error as string | undefined,
      duration_ms: tc.durationMs as number | undefined,
      containerTaskId: tc.containerTaskId as string | undefined,
    }
  })
}

/** 思考内容恢复：顶层 reasoning_content 优先，兼容旧 metadata.thinkingContent */
function deriveThinking(backendMessage: BackendMessageResponse): Message['thinking'] {
  const reasoningContent = backendMessage.reasoningContent
  const thinkingStr =
    reasoningContent ||
    ((backendMessage.metadata?.thinkingContent ?? undefined) as string | undefined)
  if (thinkingStr && typeof thinkingStr === 'string' && thinkingStr.length > 0) {
    return { content: thinkingStr, isThinking: false }
  }
  return undefined
}

/** 组装非 tool 消息的 parts[]（思考 → 正文/system → tool_call，sequence 递增） */
function assembleNonToolParts(params: {
  backendMessage: BackendMessageResponse
  isSystemMsg: boolean
  thinking: Message['thinking']
  toolCalls?: MessageToolCall[]
}): MessagePart[] {
  const { backendMessage, isSystemMsg, thinking, toolCalls } = params
  const metadata = backendMessage.metadata

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
        // 构建时若 assistant toolCalls 已带 error，则派生为 'error'；否则默认 'done'。
        // 此处 toolCalls 通常无 per-call status（后端不填充），最终 state 由
        // mergeConsecutiveAssistantMessages 根据 tool-role 消息的 toolError/status 权威派生，
        // 与流式 toolHandler.ts:142 路径一致（失败 → 'error'，成功 → 'done'）。
        state: tc.error ? 'error' : 'done',
        result: tc.result,
        resultData: tc.resultData,
        error: tc.error,
        durationMs: tc.duration_ms,
        sequence: seq++,
        // 从后端 API 恢复 containerTaskId（tc 上是 camelCase 字段——误读
        // snake_case 恒 undefined），确保历史消息加载后工具卡片的"打开文件"
        // 能正确解析工作空间路径。
        containerTaskId: tc.containerTaskId || undefined,
      })
    }
  }

  return parts
}

/**
 * 后端消息 → 前端 Message 共享映射。
 *
 * 冷热路径同构的唯一入口：DB 历史加载（getMessages）与 new_message 流式事件
 * （messageHandler）都经它生成 parts[]——流式收到的消息与刷新后从数据库加载
 * 的数据形态一致，不再有两套 parts 构造逻辑。
 *
 * 按 role 分派：tool 结果消息走独立投影；其余消息组装通用 parts[]。
 */
export function mapBackendMessageToMessage(
  backendMessage: BackendMessageResponse,
  sessionId: string,
): Message {
  if (backendMessage.role === 'tool') {
    return buildToolResultMessage(backendMessage, sessionId)
  }

  const toolCalls =
    backendMessage.toolCalls && Array.isArray(backendMessage.toolCalls)
      ? normalizePersistedToolCalls(backendMessage.toolCalls)
      : undefined
  const thinking = deriveThinking(backendMessage)
  const metadata = backendMessage.metadata
  const isSystemMsg = checkIsSystemMessage(backendMessage.role, metadata)
  const parts = assembleNonToolParts({ backendMessage, isSystemMsg, thinking, toolCalls })

  const effectiveRole = isSystemMsg ? 'system' : backendMessage.role as Message['role']

  return {
    id: backendMessage.id,
    sessionId: sessionId,
    sequence: backendMessage.sequence ?? 0,
    role: effectiveRole,
    content: backendMessage.content,
    timestamp: backendMessage.timestamp,
    agentId: backendMessage.agentId,
    // 服务端权威 status 透传（中断 interrupted / 错误 error 半截消息；
    // 正常消息缺省 completed），前端 MessageItem 据此渲染中断/失败态。
    status: (backendMessage.status || 'completed') as Message['status'],
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

/** 消除合并组内 part.sequence 的冲突，保持每条消息内 parts 的逻辑顺序
 * （渲染层 buildFragmentsFromParts 按 part.sequence 数值升序渲染）。
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

/** 合并真正连续的 assistant 消息（仅用于历史 API 加载时的同一次响应拆分场景）
 *
 * 设计意图：后端可能将同一次 LLM 响应的 text 和 tool_calls 拆成多条 ExecutionRecordData
 * （如 assistant 声明 tool_call、tool 结果、assistant 基于结果的回答是同一轮响应的拆分），
 * 本函数将这些"真正连续"（中间无 tool 消息分隔）的 assistant 合并回一个气泡，并把
 * tool 结果注入对应 tool_call part。
 *
 * 重要边界（多轮工具调用渲染）：
 *   - **tool 消息必须保留**：tool 角色消息是独立渲染的（MessageItem 有 isTool 分支），
 *     不能被合并逻辑吸收丢弃。原实现在第一遍用 `i++` 消费 tool 消息却不 push，
 *     导致工具结果消息全部丢失。
 *   - **被 tool 分隔的多轮 assistant 不得合并**：多轮工具调用中每一轮
 *     （assistant 声明 → tool 结果 → assistant 回答）是独立的气泡。
 *     原实现因 tool 被吞，多轮 assistant 在 absorbed 中变成"连续"，被第二遍
 *     合并成一条 —— 这是用户反馈"AI 消息只剩一条、工具消息不显示"的根因。
 *   - 仅当 assistant 真正相邻（无 tool 分隔，如同一次响应拆分的 thinking+text）
 *     时才合并。
 */
export function mergeConsecutiveAssistantMessages(messages: Message[]): Message[] {
  if (messages.length <= 1) return messages
  // 第一遍：将 tool 消息的结果注入前一个 assistant 的 tool_call part，
  //         同时保留 tool 消息本身（消费 tool 必须 push，否则 tool 丢失）
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
    const toolParts = (assistant.parts || []).filter(
      (p): p is ToolCallPart => p.type === 'tool_call',
    )
    i++
    // 收集 tool 消息（注入结果到 tool_call part 后保留 tool 消息本身），
    //   并在 assistant 之后入列，保持「assistant 声明 → tool 结果」的原始顺序。
    //   只 i++ 消费 tool 却不 push 会导致 tool 消息全部丢失；
    //   若在 assistant 之前 push 则顺序错乱，tool 无法分隔多轮 assistant。
    const toolMessages: Message[] = []
    while (i < messages.length && messages[i].role === 'tool') {
      const tm = messages[i]
      const tcId = tm.toolCallId
      if (tcId) {
        const target = toolParts.find((p: any) => p.callId === tcId)
        if (target) {
          target.result = tm.toolResult
          target.error = tm.toolError
          // 权威派生 tool_call part 的 state（与流式 toolHandler.ts:142 等价：
          // success === false → 'error'，否则 'done'）。后端把工具执行结果持久化为
          // tool-role 消息的 status（completed/failed）+ error；此处 tm.toolError
          // 承载后端 error（见 mapBackendMessageToMessage），tm.status 承载后端 status。
          // 任一信号表明失败 → 'error'（刷新后失败工具不得显示 completed）。
          // 备注：tm.status 运行时可为 'failed'（后端真实值），用 as string 比较规避
          // Message.status 联合类型未列 'failed' 的编译期告警。
          const failed = !!tm.toolError || (tm.status as string) === 'failed'
          target.state = failed ? 'error' : 'done'
          target.durationMs = tm.durationMs ?? target.durationMs
          // 结构化结果 envelope 注入（冷热一致性）：与流式 tool_result 事件的
          // result_data / container_task_id 同源，刷新后工具卡片的 diff 徽标、
          // 打开文件路径解析不降级。
          target.resultData = tm.toolResultData ?? target.resultData
          target.containerTaskId = tm.containerTaskId ?? target.containerTaskId
        }
      }
      toolMessages.push(tm)
      i++
    }
    absorbed.push(assistant, ...toolMessages)
  }
  // 第二遍：合并一个对话轮次为一个气泡。
  // 从一条 assistant 开始，把其后连续的 assistant/tool 交错序列（多轮工具调用）
  // 合并成一条 assistant 消息——与流式渲染一致（流式时 tool_start/tool_result
  // 把 tool_call part 追加/更新在同一个 assistant 的 parts 里，多轮天然一个气泡；
  // 刷新后历史是分离消息，合并成同构结构）。
  // 遇到下一条非 assistant/tool 消息（如 user / system）则结束本轮。
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
    while (j < absorbed.length && (absorbed[j].role === 'assistant' || absorbed[j].role === 'tool')) {
      j++
    }
    const group = absorbed.slice(groupStart, j)
    if (group.length === 1) {
      result.push(group[0])
      continue
    }
    const first = group[0]
    // 合并文本：只取 assistant 的 content（tool 的结果已注入 assistant 的 tool_call part）
    const mergedContent = group
      .filter((m) => m.role === 'assistant')
      .map((m) => m.content)
      .filter((c) => c?.trim())
      .join('\n\n')
    // 合并 parts：按消息分组传入（而非 flatMap 打平），dedupePartSequences 据此保持
    // 每条消息内 parts 的逻辑顺序（思考 → tool_call → 文本 → tool_call → 文本...）。
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
    const response = await apiClient.get<any>(API_ENDPOINTS.SESSIONS.LIST, {
      params: { session_type: 'main_pipeline', limit: 100 },
    })

    // 后端返回 {threads: [...], total: N} 格式，非纯数组
    const threads = Array.isArray(response.data)
      ? response.data
      : (response.data?.threads || [])
    return threads.map(mapThreadToSession)
  }, options)
}

export interface CreateSessionOptions {
  title?: string
  agentId?: string
  /**
   * 插件表单值整包（metadata 存储形状）。键 = 各插件 thread_fields 声明的
   * x_metadata_key（缺省 name），由表单层按声明映射——本层不感知具体字段。
   * 整体并入 metadata 落库（内核 create_session 合并默认后持久化，
   * initial_state 组装 execution_context 时按消费插件读取）。
   */
  fieldMetadata?: Record<string, string>
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

    if (options.fieldMetadata && Object.keys(options.fieldMetadata).length > 0) {
      requestData.metadata = { ...(options.fieldMetadata ?? {}) }
    }

    const response = await apiClient.post<ThreadCreateResponse>(
      API_ENDPOINTS.SESSIONS.CREATE,
      requestData,
      {
        headers: {
          'X-Main-Agent-Request': 'true',
        },
      },
    )

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
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    await apiClient.delete(API_ENDPOINTS.SESSIONS.DELETE(sessionId))
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
    after_sequence?: number
  },
  options: RetryOptions = {},
): Promise<{
  messages: Message[]
  total: number
  has_more: boolean
  /** 存活中间态（ADR 2026-08-27 §2.6）：寄存器未落库的流式快照，供刷新后重建占位。 */
  transient_states?: TransientStateEntry[]
}> {
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
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

    // 后端 MessageListResponse 始终是对象格式 {messages, total, has_more}。
    // 防御传输层异常（拦截器返回 undefined / 204 无 body 等）：早退抛明确错误，
    // 避免直接读 response.data.messages 时抛出难排查的
    // 「Cannot read properties of undefined (reading 'data')」。
    // 不能静默降级为空列表：initFromAPI 是全量替换，空列表会误清空已渲染消息。
    const payload = response?.data
    if (!payload || typeof payload !== 'object') {
      throw new Error(
        `获取消息列表失败：响应缺少数据体 (session=${sessionId}, status=${response?.status ?? 'unknown'})`,
      )
    }

    const rawMessages = payload.messages || []
    const mapped = rawMessages.map((msg: BackendMessageResponse) =>
      mapBackendMessageToMessage(msg, sessionId),
    )
    // has_more 为信封必带字段（kernel session_routes.rs 恒回 {messages,total,has_more}，
    // 空列表同样回 false）；缺失/类型不符属协议违反，fail-closed 抛错而非默认 false
    // （false 会让分页游标误判「没有更多历史」，静默丢失入口）。
    if (typeof payload.has_more !== 'boolean') {
      throw new Error(
        `获取消息列表失败：响应缺少 has_more 字段（协议违反）(session=${sessionId})`,
      )
    }
    // 数据层不合并：mergeConsecutiveAssistantMessages 会把子管道 50 条连续
    // assistant+tool 合成 1-2 条，合并后只保留组内第一条的 sequence，中间
    // sequence 丢失 → before_sequence 游标跳过中间消息 → 「加载到上面消息丢失」。
    // 合并移到渲染层，数据层保持原始消息、sequence 连续。
    return {
      messages: mapped,
      total: payload.total ?? rawMessages.length,
      has_more: payload.has_more,
      // 存活中间态透传（纯增字段向后兼容：旧后端/无中间态时缺省 undefined，
      // 消费方按无中间态处理，行为与现状一致）
      ...(Array.isArray(payload.transient_states)
        ? { transient_states: payload.transient_states as TransientStateEntry[] }
        : {}),
    }
  }, options)
}

interface ThreadUpdateRequest {
  /** 用户意图/标题（后端 intent 字段兼作会话标题） */
  intent?: string
  agent_id?: string | null
  metadata?: Record<string, unknown>
}

interface ThreadUpdateResponse {
  thread_id: string
  current_state: string
  intent: string | null
  created_at: string
  updated_at: string
  agent_id?: string | null
}

export async function updateSessionAgent(
  sessionId: string,
  agentId: string | null,
  options: RetryOptions = {},
): Promise<Session> {
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    // PATCH 返回完整的 ThreadResponse，无需二次 GET
    const response = await apiClient.patch<ThreadStateResponse>(
      API_ENDPOINTS.SESSIONS.UPDATE_AGENT(sessionId),
      { agent_id: agentId },
    )

    return mapThreadToSession(response.data)
  }, options)
}

/** 更新会话选项 */
interface UpdateSessionOptions extends RetryOptions {
  title?: string
  agentId?: string | null
  metadata?: Record<string, unknown>
}

export async function updateSession(
  sessionId: string,
  options: UpdateSessionOptions = {},
): Promise<Session> {
  const { title, agentId, metadata, ...retryOptions } = options

  validateSessionId(sessionId)

  return requestWithRetry(async () => {
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
      API_ENDPOINTS.SESSIONS.UPDATE(sessionId),
      requestData,
    )

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
