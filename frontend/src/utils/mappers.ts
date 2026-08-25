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

/**
 * 将 API 的 Thread 映射为 Session
 */
export function mapThreadToSession(thread: Thread | ThreadStateResponse): Session {
  const metadata = (thread as any).metadata || {}
  return {
    id: thread.thread_id,
    title: (thread as ThreadStateResponse).title || thread.intent || '未命名会话',
    createdAt: thread.created_at || new Date().toISOString(),
    updatedAt: thread.updated_at || new Date().toISOString(),
    messageCount: (thread as ThreadStateResponse).message_count ?? 0,
    status: (thread as any).status || thread.current_state || 'active',
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
