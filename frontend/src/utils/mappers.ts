/**
 * 数据映射工具函数
 */

import type { Thread } from '../types/api'
import type { Node, Edge, GraphData, NodeStatus } from '../types/graph'
import type { Session } from '../types/models'

/**
 * 图数据相关类型
 */
export interface BackendNodeData {
  id: string
  label: string
  status: string
  type?: string
  description?: string
  input?: any
  output?: any
  logs?: string[]
  isMainAgent?: boolean
  agentName?: string
  parentId?: string
  error?: string
  startTime?: string
  endTime?: string
  duration?: number
  [key: string]: any
}

export interface BackendEdgeData {
  id: string
  source: string
  target: string
  label?: string
  [key: string]: any
}

export interface BackendGraphData {
  nodes: BackendNodeData[]
  edges: BackendEdgeData[]
  [key: string]: any
}

/**
 * 节点状态映射
 * 将后端状态字符串映射为前端 NodeStatus 类型
 */
export function mapNodeStatus(status: string): NodeStatus {
  const statusMap: Record<string, NodeStatus> = {
    pending: 'pending',
    running: 'running',
    completed: 'completed',
    failed: 'failed',
    cancelled: 'failed', // cancelled 映射为 failed
    error: 'failed',
    success: 'completed',
  }
  return statusMap[status] || 'pending'
}

/**
 * 将后端节点数据映射为前端节点类型
 */
export function mapBackendNodeToNode(backendNode: BackendNodeData): Node {
  return {
    id: backendNode.id,
    type: (backendNode.type as any) || 'task',
    data: {
      label: backendNode.label,
      status: mapNodeStatus(backendNode.status),
      description: backendNode.description,
      input: backendNode.input,
      output: backendNode.output,
      logs: backendNode.logs,
      isMainAgent: backendNode.isMainAgent,
      agentName: backendNode.agentName,
      parentId: backendNode.parentId,
      error: backendNode.error,
      startTime: backendNode.startTime,
      endTime: backendNode.endTime,
      duration: backendNode.duration,
    },
    position: { x: 0, y: 0 }, // 默认位置，由布局算法计算
  }
}

/**
 * 将后端边数据映射为前端边类型
 */
export function mapBackendEdgeToEdge(backendEdge: BackendEdgeData): Edge {
  return {
    id: backendEdge.id,
    source: backendEdge.source,
    target: backendEdge.target,
    label: backendEdge.label,
  }
}

/**
 * 将后端图数据映射为前端图数据格式
 * 完整的类型转换，确保类型安全
 */
export function mapBackendGraphToGraphData(backendData: BackendGraphData): GraphData {
  return {
    nodes: backendData.nodes.map(mapBackendNodeToNode),
    edges: backendData.edges.map(mapBackendEdgeToEdge),
  }
}

/**
 * 后端线程状态响应类型
 * 与后端 /api/v1/threads 返回格式对齐
 */
export interface ThreadStateResponse {
  /** 线程ID */
  thread_id: string
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
}

/**
 * 将 API 的 Thread 映射为 Session
 */
export function mapThreadToSession(thread: Thread | ThreadStateResponse): Session {
  return {
    id: thread.thread_id,
    title: thread.intent || '未命名会话',
    createdAt: thread.created_at || new Date().toISOString(),
    updatedAt: thread.updated_at || new Date().toISOString(),
    messageCount: (thread as any).message_count || 0,
    status: (thread as any).status || thread.current_state || 'active',
    metadata: (thread as any).metadata || {},
    agentId: thread.agent_id || null,
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

/**
 * 线程详情响应类型
 * 与后端 /api/v1/threads/{thread_id} 返回格式对齐
 */
export interface ThreadDetailResponse {
  /** 线程ID */
  thread_id: string
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
  /** 执行图数据 */
  execution_graph?: BackendGraphData
  /** 消息数量 */
  message_count?: number
  /** 状态 */
  status?: string
  /** 元数据 */
  metadata?: Record<string, any>
}

/**
 * 将线程详情映射为执行图数据
 * 从线程详情中提取执行图数据并映射为前端格式
 */
export function mapThreadDetailToGraph(threadDetail: ThreadDetailResponse): GraphData {
  const graphData = threadDetail.execution_graph || { nodes: [], edges: [] }

  // 使用完整的类型转换函数
  return mapBackendGraphToGraphData(graphData)
}
