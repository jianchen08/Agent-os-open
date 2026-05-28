/**
 * 消息操作 API 服务
 *
 * 暴露接口：
 * - toMessage(raw, defaults): Message - 统一消息转换函数
 * - toMessageFromApi(apiResponse): Message - 将 API 响应转换为前端 Message 类型
 * - MessageResponse - 消息响应类型
 * - MessageListResponse - 消息列表响应
 */

import type { Message, MessageToolCall, ThinkingContent } from '@/types/models'
import type { MessagePart } from '@/types/messageParts'

/**
 * 消息响应类型（数据库驱动增强）
 */
export interface MessageResponse {
  id: string
  sessionId: string
  parentId?: string
  sequence: number
  role: 'user' | 'assistant' | 'system'
  agentName?: string
  content?: string
  toolCalls?: Array<{
    callId: string
    toolName: string
    toolArgs: Record<string, unknown>
    status: string
    result?: unknown
    error?: string
    startedAt?: string
    completedAt?: string
    durationMs?: number
    progress?: number
    partialOutput?: string[]
    estimatedRemainingMs?: number
    currentStep?: string
  }>
  // 消息分段信息（用于正确渲染工具卡片位置）
  toolCallId?: string
  extraData?: Record<string, unknown>
  timestamp: string

  // 发送者标识字段
  senderType?: 'user' | 'agent' | 'system'
  senderId?: string
  senderName?: string

  // Agent关联字段
  agentId?: string

  // 增强的元数据
  metadata?: {
    recordType: string
    executorType?: string
    executorId?: string
    executorName?: string
    inputData?: Record<string, unknown>
    outputData?: Record<string, unknown>
    sequence?: number
    toolCalls?: Array<{
      callId: string
      toolName: string
      toolArgs: Record<string, unknown>
      status: string
      result?: unknown
      error?: string
      startedAt?: string
      completedAt?: string
      durationMs?: number
      progress?: number
      partialOutput?: string[]
      estimatedRemainingMs?: number
      currentStep?: string
    }>
    thinkingContent?: string
    name?: string
    input?: Record<string, unknown>
    args?: Record<string, unknown>
    output?: unknown
    result?: unknown
    error?: string
    duration_ms?: number
  }
}

export function toMessage(
  raw: Record<string, unknown>,
  defaults?: { sessionId?: string; sequence?: number },
): Message {
  // 支持多种字段命名
  const id = (raw.message_id || raw.id) as string
  const sessionId = ((raw.thread_id || raw.sessionId || defaults?.sessionId) as string) || ''
  const parentId = (raw.parent_message_id || raw.parentId || raw.parent_message_id) as
    | string
    | undefined
  const sequence = (raw.sequence as number) ?? defaults?.sequence ?? 0
  const role = (raw.role || 'assistant') as 'user' | 'assistant' | 'system' | 'tool'
  const content = (raw.content as string) || ''
  const timestamp = (raw.timestamp as string) || new Date().toISOString()
  const agentId = (raw.agentId || raw.agent_id) as string | undefined

  // 思考内容：优先从顶层 thinking 字段获取，其次从 metadata.thinkingContent 恢复
  let thinking = raw.thinking as ThinkingContent | undefined
  if (!thinking) {
    const meta = raw.metadata as Record<string, unknown> | undefined
    const thinkingStr = (meta?.thinkingContent || meta?.thinking_content) as string | undefined
    if (thinkingStr && typeof thinkingStr === 'string' && thinkingStr.length > 0) {
      thinking = {
        content: thinkingStr,
        isThinking: false,
      }
    }
  }

  // BUG-FIX-fix_20260321_tool_status: 工具消息独立处理
  // 问题根因: 工具消息和 AI 消息混在一起
  // 修复方案: 工具消息独立返回，直接使用工具执行记录的 status
  const metadata = raw.metadata as Record<string, unknown> | undefined
  if (role === 'tool') {
    return {
      id,
      sessionId,
      parentId,
      sequence,
      role: 'tool',
      content,
      timestamp,
      agentId,
      status: (raw.status as string) || 'completed',
      // 工具消息特有字段（字段名与数据库一致）
      toolCallId: (raw.toolCallId || raw.tool_call_id) as string | undefined,
      toolName: (raw.toolName || raw.tool_name || metadata?.name) as string | undefined,
      toolArgs: (raw.toolArgs || raw.tool_args || metadata?.input || metadata?.args) as
        | Record<string, unknown>
        | undefined,
      toolResult: (raw.toolResult ||
        raw.tool_result ||
        metadata?.output ||
        metadata?.result) as unknown,
      toolError: (raw.toolError || raw.tool_error || metadata?.error) as string | undefined,
      durationMs: (raw.durationMs || raw.duration_ms || metadata?.duration_ms) as
        | number
        | undefined,
      metadata: metadata,
    } as any
  }

  // toolCalls 转换：统一字段命名
  let toolCalls: MessageToolCall[] | undefined
  const rawToolCalls = (raw.toolCalls || metadata?.toolCalls) as
    | Array<Record<string, unknown>>
    | undefined
  if (rawToolCalls && Array.isArray(rawToolCalls)) {
    toolCalls = rawToolCalls.map((tc) => ({
      call_id: (tc.call_id || tc.callId || tc.tool_call_id) as string,
      tool_name: (tc.tool_name || tc.toolName || tc.name) as string,
      tool_args: (tc.tool_args || tc.toolArgs || tc.args || tc.parameters || {}) as Record<
        string,
        unknown
      >,
      status: (tc.status || 'pending') as 'pending' | 'running' | 'completed' | 'failed',
      result: tc.result,
      error: tc.error as string | undefined,
      duration_ms: (tc.duration_ms || tc.durationMs) as number | undefined,
    }))
  }

  // 构建 parts[]：参照 session.ts 中 mapBackendMessageToMessage 的 parts 构建逻辑
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

  // BUG-FIX-fix_20260528_system_msg_render:
  // 问题根因: 仅匹配 '[系统通知]' 前缀，遗漏 '[系统提醒]' 等其他系统消息
  // 修复方案: 改为匹配 '[系统' 开头的所有前缀
  const isSystemMsg =
    role === 'system' ||
    metadata?.record_type === 'system' ||
    metadata?.type === 'system' ||
    metadata?.sender_type === 'system' ||
    content?.trimStart().startsWith('[系统')

  if (content?.trim()) {
    if (isSystemMsg) {
      parts.push({
        type: 'system',
        content,
        level: (metadata?.notification_level as any) || 'info',
        notificationType: (metadata?.notification_type as string) || 'task_notification',
        sequence: seq++,
      })
    } else {
      parts.push({
        type: 'text',
        content,
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
      })
    }
  }

  // BUG-FIX-fix_20260528_system_msg_render:
  // 问题根因: 后端引擎将系统通知存储为 type='user'，API 返回 role='user'，
  //           导致前端合并后系统消息丢失系统样式
  // 修复方案: 当 isSystemMsg 为 true 时，将 role 修正为 'system'
  const effectiveRole = isSystemMsg ? 'system' : role

  return {
    id,
    sessionId,
    parentId,
    sequence,
    role: effectiveRole as any,
    content,
    timestamp,
    agentId,
    thinking,
    // toolCalls 已迁移到 parts[]，不再单独赋值
    metadata: raw.metadata as Record<string, unknown> | undefined,
    parts: parts.length > 0 ? parts : undefined,
  }
}

/**
 * 将 API 响应转换为前端 Message 类型
 */
export function toMessageFromApi(apiResponse: MessageResponse): Message {
  return toMessage(apiResponse as unknown as Record<string, unknown>, {
    sequence: apiResponse.sequence,
  })
}

/**
 * 消息列表响应
 */
export interface MessageListResponse {
  messages: MessageResponse[]
  total: number
  session_id: string
}
