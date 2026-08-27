/**
 * 数据映射工具函数
 */

import type { Thread } from '../types/api'
import type { Session } from '../types/models'

/**
 * 后端线程状态响应类型
 * 与后端 /api/v1/sessions 返回格式对齐
 */
export interface ThreadStateResponse {
  /** 线程ID */
  thread_id: string
  /** 线程标题 */
  title?: string | null
  /** 当前状态 */
  current_state: string
  /** 用户意图（会话标题） */
  intent: string | null
  /** 创建时间 */
  created_at: string
  /** 更新时间 */
  updated_at: string
  /** 绑定的 Agent ID */
  agent_id?: string | null
  /** 会话工作空间绝对路径 */
  workspace?: string | null
  /** 会话隔离模式 */
  isolation_mode?: 'isolated' | 'non_isolated' | null
  /** 消息数量 */
  message_count?: number
  /** 关联的管道 ID 列表 */
  pipeline_ids?: string[]
  /** 当前活跃的管道 ID */
  active_pipeline_id?: string | null
  /** 元数据 */
  metadata?: Record<string, any>
}

/** 会话必返时间戳缺失 → 协议违反上抛（用当前时间伪造会让排序/相对时间失真） */
function requireIsoTimestamp(value: string | undefined, threadId: string, field: string): string {
  if (!value) {
    throw new Error(`会话数据缺少 ${field} 字段（协议违反）: thread=${threadId}`)
  }
  return value
}

/**
 * 将 API 的 Thread 映射为 Session
 *
 * - title：后端 title/intent 均可为 null（未命名会话）；Session.title 契约为非空
 *   string 且全部 UI 消费方按标题渲染——此处回退「未命名会话」为展示占位文案，
 *   仅作用于渲染标签，不写回任何数据字段。
 * - status：legacy Thread.status 非两型共有契约；current_state 是权威状态源。
 *   两者皆缺时置 undefined（Session.status 可选），不伪造 'active'。
 * - 时间戳：created_at/updated_at 为后端必返字段，缺失抛协议错误。
 */
export function mapThreadToSession(thread: Thread | ThreadStateResponse): Session {
  const metadata = thread.metadata || {}
  const legacyStatus = 'status' in thread ? thread.status : undefined
  return {
    id: thread.thread_id,
    title: (thread as ThreadStateResponse).title || thread.intent || '未命名会话',
    createdAt: requireIsoTimestamp(thread.created_at, thread.thread_id, 'created_at'),
    updatedAt: requireIsoTimestamp(thread.updated_at, thread.thread_id, 'updated_at'),
    messageCount: (thread as ThreadStateResponse).message_count ?? 0,
    status: legacyStatus || thread.current_state || undefined,
    metadata: metadata,
    agentId: thread.agent_id || null,
    workspace: (thread as ThreadStateResponse).workspace ?? null,
    isolationMode: (thread as ThreadStateResponse).isolation_mode ?? null,
    pipelineIds: (thread as ThreadStateResponse).pipeline_ids || [],
    activePipelineId: (thread as ThreadStateResponse).active_pipeline_id || null,
    pinned: metadata.pinned === true,
    starred: metadata.starred === true,
  }
}

/**
 * 会话主管道的权威解析（activePipelineId 优先，不按 [0] 位置猜测）：
 * - 优先后端权威 activePipelineId（session_routes 回显，内核 resolve 同源）；
 * - 缺失（旧数据）且 pipelineIds 恰一个元素 → 取 [0]（无歧义）；
 * - 缺失且多元素 → undefined（不猜位置序号，调用方 fail-closed 拒绝/中止）。
 */
export function mainPipelineIdOf(session: {
  activePipelineId?: string | null
  pipelineIds?: string[]
}): string | undefined {
  if (session.activePipelineId) return session.activePipelineId
  if (session.pipelineIds && session.pipelineIds.length === 1) {
    return session.pipelineIds[0]
  }
  return undefined
}
