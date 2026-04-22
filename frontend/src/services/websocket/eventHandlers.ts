/**
 * WebSocket 事件处理器模块
 *
 * 提供类型安全的事件订阅和分发机制
 *
 * 暴露接口：
 * - EventHandlerManager - 事件处理器管理器类
 * - createEventHandlerManager() - 创建事件处理器管理器实例
 * - EventHandler - 事件处理器类型
 * - EventHandlerMap - 事件处理器映射类型
 * - ServerEventDataMap - 服务端事件数据类型映射
 */

import { WS_SERVER_EVENTS } from '@/constants/websocket'

/**
 * 事件处理器类型
 */
export type EventHandler<T = unknown> = (data: T) => void

/**
 * 事件处理器映射类型
 */
export type EventHandlerMap = Map<string, Set<EventHandler>>

/**
 * 服务端事件数据类型映射
 */
export interface ServerEventDataMap {
  [WS_SERVER_EVENTS.CONNECTION_ESTABLISHED]: {
    connection_id: string
    thread_id: string
  }
  [WS_SERVER_EVENTS.STATE_CHANGE]: {
    previous_state: string
    current_state: string
    thread_id: string
  }
  [WS_SERVER_EVENTS.APPROVAL_REQUIRED]: {
    approval_id: string
    content: unknown
    thread_id: string
  }
  [WS_SERVER_EVENTS.TASK_COMPLETED]: {
    result: unknown
    thread_id: string
  }
  [WS_SERVER_EVENTS.TASK_CANCELLED]: {
    reason: string
    thread_id: string
  }
  [WS_SERVER_EVENTS.ERROR]: {
    error_code: string
    message: string
    thread_id: string
  }
  [WS_SERVER_EVENTS.HEARTBEAT]: {
    timestamp?: number
  }
  [WS_SERVER_EVENTS.NEW_MESSAGE]: {
    message: {
      id: string
      sessionId: string
      role: string
      content: string
      timestamp: string
      metadata?: Record<string, unknown>
    }
  }
  [WS_SERVER_EVENTS.STREAM_CHUNK]: {
    messageId: string
    chunk: string
    threadId: string
  }
  [WS_SERVER_EVENTS.STREAM_END]: {
    messageId: string
    threadId: string
    metadata?: Record<string, unknown>
  }
  [WS_SERVER_EVENTS.THINKING_START]: {
    messageId: string
    threadId: string
  }
  [WS_SERVER_EVENTS.THINKING_CHUNK]: {
    messageId: string
    chunk: string
    threadId: string
  }
  [WS_SERVER_EVENTS.THINKING_END]: {
    messageId: string
    threadId: string
    durationMs?: number
  }
  [WS_SERVER_EVENTS.EXECUTION_START]: {
    execution_id: string
    execution_type: 'tool' | 'agent' | 'workflow'
    name: string
    description?: string
    parent_id?: string
    input?: Record<string, unknown>
    metadata?: Record<string, unknown>
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_PROGRESS]: {
    execution_id: string
    progress: number
    current_step?: string
    message?: string
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_DONE]: {
    execution_id: string
    success: boolean
    output?: Record<string, unknown>
    error?: string
    duration_ms?: number
    summary?: string
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_CANCELLED]: {
    execution_id: string
    reason: string
    cancelled_by?: 'user' | 'system' | 'timeout'
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_OUTPUT]: {
    execution_id: string
    output: string
    append: boolean
    timestamp: string
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_STATUS_UPDATE]: {
    execution_id: string
    status: string
    progress?: number
    thread_id?: string
  }
  [WS_SERVER_EVENTS.SUB_AGENT_INPUT_REQUEST]: {
    execution_id: string
    agent_id: string
    prompt: string
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_EVENT]: {
    execution_id: string
    event_type: string
    data: unknown
    thread_id?: string
  }
  [WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE]: {
    execution_id: string
    step_id: string
    step_name: string
    status: string
    output?: Record<string, unknown>
    thread_id?: string
  }
  [WS_SERVER_EVENTS.EXECUTION_CONTROL_RESPONSE]: {
    execution_id: string
    action: 'pause' | 'resume' | 'cancel' | 'rollback'
    success: boolean
    message: string
    new_status?: string
    thread_id?: string
  }
  [WS_SERVER_EVENTS.AGENT_INJECT_RESPONSE]: {
    execution_id: string
    agent_id: string
    success: boolean
    message: string
    thread_id?: string
  }
}

/**
 * 事件处理器管理器
 *
 * 提供类型安全的事件订阅、取消订阅和分发功能
 */
export class EventHandlerManager {
  /** 事件处理器映射 */
  private handlers: EventHandlerMap = new Map()

  /**
   * 订阅类型安全的事件
   */
  subscribe<K extends keyof ServerEventDataMap>(
    event: K,
    handler: EventHandler<ServerEventDataMap[K]>
  ): void

  /**
   * 订阅通用事件
   */
  subscribe(event: string, handler: EventHandler): void

  /**
   * 订阅事件
   *
   * @param event 事件类型
   * @param handler 事件处理器
   */
  subscribe(event: string, handler: EventHandler): void {
    if (!this.handlers.has(event)) {
      this.handlers.set(event, new Set())
    }
    this.handlers.get(event)!.add(handler)
  }

  /**
   * 取消订阅事件
   *
   * @param event 事件类型
   * @param handler 事件处理器
   */
  unsubscribe(event: string, handler: EventHandler): void {
    const handlers = this.handlers.get(event)
    if (handlers) {
      handlers.delete(handler)
      if (handlers.size === 0) {
        this.handlers.delete(event)
      }
    }
  }

  /**
   * 取消订阅某事件的所有处理器
   *
   * @param event 事件类型
   */
  unsubscribeAll(event: string): void {
    this.handlers.delete(event)
  }

  /**
   * 清除所有订阅
   */
  clear(): void {
    this.handlers.clear()
  }

  /**
   * 触发类型安全的事件
   */
  emit<K extends keyof ServerEventDataMap>(
    event: K,
    data: ServerEventDataMap[K]
  ): void

  /**
   * 触发通用事件
   */
  emit(event: string, data: unknown): void

  /**
   * 触发事件
   *
   * @param event 事件类型
   * @param data 事件数据
   */
  emit(event: string, data: unknown): void {
    const handlers = this.handlers.get(event)
    if (handlers) {
      handlers.forEach(handler => {
        try {
          handler(data)
        } catch (error) {
          console.error(`[EventHandler] 事件处理器执行失败 (${event}):`, error)
        }
      })
    }
  }

  /**
   * 检查是否有订阅者
   *
   * @param event 事件类型
   * @returns 是否有订阅者
   */
  hasSubscribers(event: string): boolean {
    const handlers = this.handlers.get(event)
    return handlers !== undefined && handlers.size > 0
  }

  /**
   * 获取订阅者数量
   *
   * @param event 事件类型
   * @returns 订阅者数量
   */
  getSubscriberCount(event: string): number {
    return this.handlers.get(event)?.size ?? 0
  }

  /**
   * 获取所有已订阅的事件类型
   *
   * @returns 事件类型数组
   */
  getSubscribedEvents(): string[] {
    return Array.from(this.handlers.keys())
  }
}

/**
 * 创建事件处理器管理器实例
 *
 * @returns 新的事件处理器管理器实例
 */
export function createEventHandlerManager(): EventHandlerManager {
  return new EventHandlerManager()
}
