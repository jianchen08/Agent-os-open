/**
 * 统一事件处理器
 *
 * 提供统一的事件订阅、取消订阅和消息发送功能。
 * 已切换到连接池模式，不再直接订阅 webSocketService。
 *
 * 注意：流式事件（stream_start/chunk/end、thinking、tool 等）
 * 已由 streamingEventService.ts 统一处理，此处理器仅保留
 * 非流式事件的桥接功能。
 */

import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { wsPool } from './WebSocketConnectionPool'
import type {
  Attachment,
  UnifiedEventPayload,
  UnifiedStreamEvent,
  UnifiedEventType,
  UnifiedEventSubscriber,
  UnifiedEventHandlerOptions,
} from './messageTypes'

export interface IUnifiedEventHandler {
  subscribe<T extends UnifiedEventType>(eventType: T, handler: UnifiedEventSubscriber<T>): void
  unsubscribe<T extends UnifiedEventType>(eventType: T, handler: UnifiedEventSubscriber<T>): void
  unsubscribeAll(eventType: UnifiedEventType): void
  sendUserInput(
    content: string,
    options?: {
      enableThinking?: boolean
      attachments?: Attachment[]
    },
  ): Promise<string>
  isConnected(): boolean
  clearAll(): void
}

export class UnifiedEventHandler implements IUnifiedEventHandler {
  private subscribers: Map<UnifiedEventType, Set<UnifiedEventSubscriber<UnifiedEventType>>> =
    new Map()
  private initialized = false
  private options: Required<UnifiedEventHandlerOptions>

  constructor(options: UnifiedEventHandlerOptions = {}) {
    this.options = {
      autoReconnect: options.autoReconnect ?? true,
      enableLogging: options.enableLogging ?? true,
      messageTimeout: options.messageTimeout ?? 30000,
    }

    this.initialize()
  }

  private initialize(): void {
    if (this.initialized) {
      return
    }

    this.log('初始化统一事件处理器（连接池模式）...')

    wsPool.subscribe(WS_SERVER_EVENTS.EXECUTION_DONE, this.handleExecutionDone.bind(this))
    wsPool.subscribe(WS_SERVER_EVENTS.ERROR, this.handleError.bind(this))
    wsPool.subscribe(WS_SERVER_EVENTS.NEW_MESSAGE, this.handleNewMessage.bind(this))

    this.initialized = true
    this.log('统一事件处理器初始化完成')
  }

  subscribe<T extends UnifiedEventType>(eventType: T, handler: UnifiedEventSubscriber<T>): void {
    if (!this.subscribers.has(eventType)) {
      this.subscribers.set(eventType, new Set())
    }
    this.subscribers.get(eventType)!.add(handler as UnifiedEventSubscriber<UnifiedEventType>)
  }

  unsubscribe<T extends UnifiedEventType>(eventType: T, handler: UnifiedEventSubscriber<T>): void {
    const handlers = this.subscribers.get(eventType)
    if (handlers) {
      handlers.delete(handler as UnifiedEventSubscriber<UnifiedEventType>)
      if (handlers.size === 0) {
        this.subscribers.delete(eventType)
      }
    }
  }

  unsubscribeAll(eventType: UnifiedEventType): void {
    this.subscribers.delete(eventType)
  }

  async sendUserInput(
    content: string,
    options?: {
      enableThinking?: boolean
      attachments?: Attachment[]
    },
  ): Promise<string> {
    const activeThread = wsPool.getActiveThread()
    if (!activeThread) return ''

    const result = await wsPool.sendUserInput(
      activeThread,
      content,
      options?.attachments,
      options?.enableThinking,
    )

    return result?.messageId ?? ''
  }

  isConnected(): boolean {
    return wsPool.hasAnyConnection()
  }

  clearAll(): void {
    this.subscribers.clear()
  }

  private emit(event: UnifiedStreamEvent): void {
    const handlers = this.subscribers.get(event.event_type)
    if (handlers) {
      handlers.forEach((handler) => {
        try {
          handler(event)
        } catch (error) {
          console.error(`[UnifiedEventHandler] 事件处理器执行失败 (${event.event_type}):`, error)
        }
      })
    }
  }

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

  private handleError(data: unknown): void {
    const errorData = data as { error_code: string; message: string; thread_id?: string }
    const event = this.createUnifiedEvent('stream.error', data, {
      error_code: errorData.error_code,
      error_message: errorData.message,
    })
    this.emit(event)
  }

  private handleNewMessage(data: unknown): void {
    const messageData = data as { message: { id: string } }
    if (messageData.message?.id) {
      this.log('收到新消息确认 | messageId:', messageData.message.id)
    }
  }

  private createUnifiedEvent(
    eventType: UnifiedEventType,
    data: unknown,
    payload: Partial<UnifiedEventPayload>,
    additionalMetadata?: Record<string, unknown>,
  ): UnifiedStreamEvent {
    const baseData = data as {
      messageId?: string
      message_id?: string
      threadId?: string
      thread_id?: string
    }

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

  private log(message: string, data?: unknown): void {
    if (this.options.enableLogging) {
      console.log(`[UnifiedEventHandler] ${message}`, data ?? '')
    }
  }
}

export function createUnifiedEventHandler(
  options?: UnifiedEventHandlerOptions,
): UnifiedEventHandler {
  return new UnifiedEventHandler(options)
}

export const unifiedEventHandler = new UnifiedEventHandler()
