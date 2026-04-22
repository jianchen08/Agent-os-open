/**
 * 统一事件处理器
 *
 * 提供统一的事件订阅、取消订阅和消息发送功能
 * 支持新的统一事件格式
 */

import { webSocketService } from './WebSocketService'
import type {
  Attachment,
  UnifiedEventPayload,
  UnifiedStreamEvent,
  UnifiedEventType,
  UnifiedEventSubscriber,
  UnifiedEventHandlerOptions,
} from './messageTypes'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

/**
 * 统一事件处理器接口
 *
 * 定义统一事件处理器的核心功能
 */
export interface IUnifiedEventHandler {
  /**
   * 订阅统一事件
   *
   * @param eventType 事件类型
   * @param handler 事件处理器函数
   */
  subscribe<T extends UnifiedEventType>(
    eventType: T,
    handler: UnifiedEventSubscriber<T>
  ): void

  /**
   * 取消订阅统一事件
   *
   * @param eventType 事件类型
   * @param handler 事件处理器函数
   */
  unsubscribe<T extends UnifiedEventType>(
    eventType: T,
    handler: UnifiedEventSubscriber<T>
  ): void

  /**
   * 取消订阅某事件类型的所有处理器
   *
   * @param eventType 事件类型
   */
  unsubscribeAll(eventType: UnifiedEventType): void

  /**
   * 发送用户输入（统一格式）
   *
   * @param content 消息内容
   * @param options 可选参数
   * @returns Promise<string> 返回消息ID
   */
  sendUserInput(
    content: string,
    options?: {
      enableThinking?: boolean
      attachments?: Attachment[]
    }
  ): Promise<string>

  /**
   * 检查是否已连接
   *
   * @returns 是否已连接
   */
  isConnected(): boolean

  /**
   * 清除所有订阅
   */
  clearAll(): void
}

/**
 * 统一事件处理器实现
 *
 * 基于现有的 WebSocketService 实现，提供统一的事件处理接口
 */
export class UnifiedEventHandler implements IUnifiedEventHandler {
  /** 订阅器映射表 */
  private subscribers: Map<UnifiedEventType, Set<UnifiedEventSubscriber<UnifiedEventType>>> =
    new Map()

  /** 是否已初始化 */
  private initialized = false

  /** 选项配置 */
  private options: Required<UnifiedEventHandlerOptions>

  /**
   * 创建统一事件处理器
   *
   * @param options 选项配置
   */
  constructor(options: UnifiedEventHandlerOptions = {}) {
    // 设置默认选项
    this.options = {
      autoReconnect: options.autoReconnect ?? true,
      enableLogging: options.enableLogging ?? true,
      messageTimeout: options.messageTimeout ?? 30000,
    }

    // 延迟初始化，避免在构造函数中调用 WebSocket 方法
    this.initialize()
  }

  /**
   * 初始化事件处理器
   *
   * 订阅 WebSocket 的底层事件，转换为统一格式
   */
  private initialize(): void {
    if (this.initialized) {
      return
    }

    this.log('初始化统一事件处理器...')

    // 订阅流式输出片段事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.STREAM_CHUNK,
      this.handleStreamChunk.bind(this)
    )

    // 订阅流式输出结束事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.STREAM_END,
      this.handleStreamEnd.bind(this)
    )

    // 订阅思考开始事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.THINKING_START,
      this.handleThinkingStart.bind(this)
    )

    // 订阅思考片段事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.THINKING_CHUNK,
      this.handleThinkingChunk.bind(this)
    )

    // 订阅思考结束事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.THINKING_END,
      this.handleThinkingEnd.bind(this)
    )

    // 订阅执行开始事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.EXECUTION_START,
      this.handleExecutionStart.bind(this)
    )

    // 订阅执行进度事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.EXECUTION_PROGRESS,
      this.handleExecutionProgress.bind(this)
    )

    // 订阅执行完成事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.EXECUTION_DONE,
      this.handleExecutionDone.bind(this)
    )

    // 订阅错误事件
    webSocketService.subscribe(
      WS_SERVER_EVENTS.ERROR,
      this.handleError.bind(this)
    )

    // 订阅新消息事件（用于获取消息ID）
    webSocketService.subscribe(
      WS_SERVER_EVENTS.NEW_MESSAGE,
      this.handleNewMessage.bind(this)
    )

    this.initialized = true
    this.log('统一事件处理器初始化完成')
  }

  /**
   * 订阅统一事件
   *
   * @param eventType 事件类型
   * @param handler 事件处理器函数
   */
  subscribe<T extends UnifiedEventType>(
    eventType: T,
    handler: UnifiedEventSubscriber<T>
  ): void {
    if (!this.subscribers.has(eventType)) {
      this.subscribers.set(eventType, new Set())
    }
    this.subscribers.get(eventType)!.add(handler as UnifiedEventSubscriber<UnifiedEventType>)
    this.log(`订阅事件: ${eventType}, 订阅者数量: ${this.getSubscriberCount(eventType)}`)
  }

  /**
   * 取消订阅统一事件
   *
   * @param eventType 事件类型
   * @param handler 事件处理器函数
   */
  unsubscribe<T extends UnifiedEventType>(
    eventType: T,
    handler: UnifiedEventSubscriber<T>
  ): void {
    const handlers = this.subscribers.get(eventType)
    if (handlers) {
      handlers.delete(handler as UnifiedEventSubscriber<UnifiedEventType>)
      if (handlers.size === 0) {
        this.subscribers.delete(eventType)
      }
      this.log(`取消订阅事件: ${eventType}, 剩余订阅者: ${handlers.size}`)
    }
  }

  /**
   * 取消订阅某事件类型的所有处理器
   *
   * @param eventType 事件类型
   */
  unsubscribeAll(eventType: UnifiedEventType): void {
    const count = this.getSubscriberCount(eventType)
    this.subscribers.delete(eventType)
    this.log(`取消订阅事件 ${eventType} 的所有处理器 (${count}个)`)
  }

  /**
   * 发送用户输入（统一格式）
   *
   * @param content 消息内容
   * @param options 可选参数
   * @returns Promise 返回消息ID
   */
  async sendUserInput(
    content: string,
    options?: {
      enableThinking?: boolean
      attachments?: Attachment[]
    }
  ): Promise<string> {
    this.log('发送用户输入:', content.substring(0, 50) + (content.length > 50 ? '...' : ''))

    const result = await webSocketService.sendUserInput(
      content,
      options?.attachments,
      options?.enableThinking
    )

    return result ?? ''
  }

  /**
   * 检查是否已连接
   *
   * @returns 是否已连接
   */
  isConnected(): boolean {
    return webSocketService.isConnected()
  }

  /**
   * 清除所有订阅
   */
  clearAll(): void {
    const totalSubscribers = Array.from(this.subscribers.values()).reduce(
      (sum, handlers) => sum + handlers.size,
      0
    )
    this.subscribers.clear()
    this.log(`清除所有订阅 (${totalSubscribers}个订阅者)`)
  }

  /**
   * 获取订阅者数量
   *
   * @param eventType 事件类型
   * @returns 订阅者数量
   */
  getSubscriberCount(eventType: UnifiedEventType): number {
    return this.subscribers.get(eventType)?.size ?? 0
  }

  /**
   * 触发事件
   *
   * @param event 统一流式事件
   */
  private emit(event: UnifiedStreamEvent): void {
    const handlers = this.subscribers.get(event.event_type)
    if (handlers) {
      handlers.forEach(handler => {
        try {
          handler(event)
        } catch (error) {
          console.error(`[UnifiedEventHandler] 事件处理器执行失败 (${event.event_type}):`, error)
        }
      })
    }
  }

  /**
   * 处理流式输出片段事件
   *
   * @param data 原始事件数据
   */
  private handleStreamChunk(data: unknown): void {
    const chunkData = data as { messageId: string; chunk: string; threadId: string }
    const event = this.createUnifiedEvent('stream.chunk', data, {
      content: chunkData.chunk,
    })
    this.emit(event)
  }

  /**
   * 处理流式输出结束事件
   *
   * @param data 原始事件数据
   */
  private handleStreamEnd(data: unknown): void {
    const endData = data as { messageId: string; threadId: string; metadata?: Record<string, unknown> }
    const event = this.createUnifiedEvent('stream.end', data, {
      final_content: '',
    }, endData.metadata)
    this.emit(event)
  }

  /**
   * 处理思考开始事件
   *
   * @param data 原始事件数据
   */
  private handleThinkingStart(data: unknown): void {
    const event = this.createUnifiedEvent('thinking.start', data, {})
    this.emit(event)
  }

  /**
   * 处理思考片段事件
   *
   * @param data 原始事件数据
   */
  private handleThinkingChunk(data: unknown): void {
    const chunkData = data as { messageId: string; chunk: string; threadId: string }
    const event = this.createUnifiedEvent('thinking.chunk', data, {
      thinking_content: chunkData.chunk,
    })
    this.emit(event)
  }

  /**
   * 处理思考结束事件
   *
   * @param data 原始事件数据
   */
  private handleThinkingEnd(data: unknown): void {
    const endData = data as { messageId: string; threadId: string; durationMs?: number }
    const event = this.createUnifiedEvent('thinking.end', data, {}, {
      duration_ms: endData.durationMs,
    })
    this.emit(event)
  }

  /**
   * 处理执行开始事件
   *
   * @param data 原始事件数据
   */
  private handleExecutionStart(data: unknown): void {
    const execData = data as {
      execution_id: string
      name: string
      thread_id?: string
      input?: Record<string, unknown>
    }
    const event = this.createUnifiedEvent('tool.start', data, {
      tool_name: execData.name,
      args: execData.input,
    })
    this.emit(event)
  }

  /**
   * 处理执行进度事件
   *
   * @param data 原始事件数据
   */
  private handleExecutionProgress(data: unknown): void {
    const progressData = data as {
      execution_id: string
      progress: number
      current_step?: string
      message?: string
      thread_id?: string
    }
    const event = this.createUnifiedEvent('tool.progress', data, {
      progress: progressData.progress,
      current_step: progressData.current_step,
    })
    this.emit(event)
  }

  /**
   * 处理执行完成事件
   *
   * @param data 原始事件数据
   */
  private handleExecutionDone(data: unknown): void {
    const doneData = data as {
      execution_id: string
      name: string
      success: boolean
      output?: Record<string, unknown>
      error?: string
      thread_id?: string
      duration_ms?: number
    }
    const event = this.createUnifiedEvent('tool.end', data, {
      tool_name: doneData.name,
      result: doneData.output,
      error: doneData.error,
    })
    this.emit(event)
  }

  /**
   * 处理错误事件
   *
   * @param data 原始事件数据
   */
  private handleError(data: unknown): void {
    const errorData = data as { error_code: string; message: string; thread_id?: string }
    const event = this.createUnifiedEvent('stream.error', data, {
      error_code: errorData.error_code,
      error_message: errorData.message,
    })
    this.emit(event)
  }

  /**
   * 处理新消息事件（用于获取消息ID）
   *
   * @param data 原始事件数据
   */
  private handleNewMessage(data: unknown): void {
    // 前端生成的UUID直接作为最终ID，无需等待后端返回
    const messageData = data as { message: { id: string } }
    if (messageData.message?.id) {
      this.log('收到新消息确认 | messageId:', messageData.message.id)
    }
  }

  /**
   * 创建统一事件
   *
   * @param eventType 事件类型
   * @param data 原始数据
   * @param payload 事件载荷
   * @param additionalMetadata 额外元数据
   * @returns 统一流式事件
   */
  private createUnifiedEvent(
    eventType: UnifiedEventType,
    data: unknown,
    payload: Partial<UnifiedEventPayload>,
    additionalMetadata?: Record<string, unknown>
  ): UnifiedStreamEvent {
    // 从数据中提取必需字段
    const baseData = data as { messageId?: string; message_id?: string; threadId?: string; thread_id?: string }

    const messageId = baseData.messageId || baseData.message_id || ''
    const threadId = baseData.threadId || baseData.thread_id || ''

    return {
      event_type: eventType,
      message_id: messageId,
      thread_id: threadId,
      payload: {
        ...payload,
      },
      metadata: {
        timestamp: new Date().toISOString(),
        chunk_index: 0,
        ...additionalMetadata,
      },
    }
  }

  /**
   * 记录日志
   *
   * @param message 日志消息
   * @param data 额外数据
   */
  private log(message: string, data?: unknown): void {
    if (this.options.enableLogging) {
      console.log(`[UnifiedEventHandler] ${message}`, data ?? '')
    }
  }
}

/**
 * 创建统一事件处理器实例
 *
 * @param options 选项配置
 * @returns 统一事件处理器实例
 */
export function createUnifiedEventHandler(
  options?: UnifiedEventHandlerOptions
): UnifiedEventHandler {
  return new UnifiedEventHandler(options)
}

/** 统一事件处理器单例实例 */
export const unifiedEventHandler = new UnifiedEventHandler()
