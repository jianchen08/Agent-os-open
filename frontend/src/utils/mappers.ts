/**
 * 数据映射工具函数
 */

import type { Thread } from '../types/api'
import type { Session } from '../types/models'

/**
 * 后端线程状态响应类型
 * 与后端 /api/v1/threads 返回格式对齐
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
    pipelineIds: (thread as ThreadStateResponse).pipeline_ids || [],
    activePipelineId: (thread as ThreadStateResponse).active_pipeline_id || null,
    pinned: metadata.pinned === true,
    starred: metadata.starred === true,
  }
}

/**
 * 批量映射 Thread 到 Session
 */
export function mapThreadsToSessions(threads: Thread[]): Session[] {
  return threads.map(mapThreadToSession)
}

/**
 * 将 Session 映射为 API Thread 格式
 */
export function mapSessionToThread(session: Session): Thread {
  return {
    thread_id: session.id,
    intent: session.title,
    current_state: session.status || 'active',
    created_at: session.createdAt,
    updated_at: session.updatedAt,
    message_count: session.messageCount,
    status: session.status,
    metadata: session.metadata,
    agent_id: session.agentId || null,
  }
}
