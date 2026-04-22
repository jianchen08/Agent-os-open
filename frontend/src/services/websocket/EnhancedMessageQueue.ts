/**
 * 增强版WebSocket消息队列
 *
 * 提供以下功能：
 * - IndexedDB持久化存储
 * - 指数退避重试机制（1s → 2s → 4s → 最大10s）
 * - 离线消息缓存
 * - 重连后自动恢复发送
 * - 消息状态追踪（pending/sent/failed）
 *
 * @module EnhancedMessageQueue
 */

import Dexie from 'dexie'
import type { Table } from 'dexie'

/**
 * 消息状态枚举
 */
export enum MessageStatus {
  /** 等待发送 */
  PENDING = 'pending',
  /** 发送中 */
  SENDING = 'sending',
  /** 已发送 */
  SENT = 'sent',
  /** 发送失败 */
  FAILED = 'failed',
}

/**
 * 队列消息接口（IndexedDB存储格式）
 */
export interface QueuedMessage {
  /** 消息唯一ID */
  id: string
  /** 消息内容（已序列化的字符串） */
  content: string
  /** 消息状态 */
  status: MessageStatus
  /** 重试次数 */
  retries: number
  /** 最大重试次数 */
  maxRetries: number
  /** 创建时间戳 */
  timestamp: number
  /** 下次重试时间戳 */
  nextRetryTime?: number
  /** 优先级（0-3，数字越大优先级越高） */
  priority: number
}

/**
 * 消息发送选项
 */
export interface SendOptions {
  /** 最大重试次数（默认3次） */
  maxRetries?: number
  /** 优先级（默认1） */
  priority?: number
  /** 发送成功回调 */
  onSent?: (messageId: string) => void
  /** 发送失败回调 */
  onFailed?: (messageId: string, error: Error) => void
}

/**
 * 消息优先级常量
 */
export const MessagePriority = {
  /** 低优先级 - 普通消息 */
  LOW: 0,
  /** 普通优先级 - 默认 */
  NORMAL: 1,
  /** 高优先级 - 重要消息 */
  HIGH: 2,
  /** 紧急优先级 - 心跳、控制消息 */
  URGENT: 3,
} as const

export type MessagePriorityType =
  (typeof MessagePriority)[keyof typeof MessagePriority]

/**
 * 消息队列配置
 */
export interface MessageQueueConfig {
  /** 数据库名称 */
  dbName: string
  /** 最大重试次数 */
  maxRetries: number
  /** 基础重试延迟（毫秒） */
  baseRetryDelay: number
  /** 最大重试延迟（毫秒） */
  maxRetryDelay: number
  /** 重试退避因子 */
  retryBackoffFactor: number
  /** 是否启用持久化 */
  enablePersistence: boolean
  /** 最大队列长度 */
  maxSize?: number
  /** 默认消息超时（毫秒） */
  defaultTimeout?: number
  /** 是否启用优先级排序 */
  enablePriority?: boolean
}

/**
 * 默认配置
 */
const DEFAULT_CONFIG: MessageQueueConfig = {
  dbName: 'WebSocketMessageQueue',
  maxRetries: 3,
  baseRetryDelay: 1000,
  maxRetryDelay: 10000,
  retryBackoffFactor: 2,
  enablePersistence: true,
}

/**
 * IndexedDB数据库接口
 */
class MessageQueueDatabase extends Dexie {
  messages!: Table<QueuedMessage, string>

  /**
   * 创建消息队列数据库实例
   *
   * @param dbName 数据库名称
   */
  constructor(dbName: string) {
    super(dbName)
    this.version(1).stores({
      messages: 'id, status, timestamp, nextRetryTime, priority',
    })
  }
}

/**
 * 增强版消息队列类
 *
 * 使用IndexedDB进行持久化存储，支持离线消息缓存和自动重试
 */
export class EnhancedMessageQueue {
  /** 数据库实例 */
  private db: MessageQueueDatabase

  /** 配置 */
  private config: MessageQueueConfig

  /** 发送函数 */
  private sender: (content: string) => Promise<boolean>

  /** 是否正在处理队列 */
  private isProcessing: boolean = false

  /** 处理定时器 */
  private processTimer: ReturnType<typeof setTimeout> | null = null

  /** 是否已停止（用于防止异步操作继续执行） */
  private stopped: boolean = false

  /** 是否暂停处理 */
  private paused: boolean = false

  /** 回调映射（内存中缓存，用于异步回调） */
  private callbacks: Map<string, SendOptions> = new Map()

  /** 消息发送处理器 */
  private sendHandler?: (content: string) => Promise<void>

  /**
   * 创建增强版消息队列
   *
   * @param sender 消息发送函数，返回Promise<boolean>表示成功/失败
   * @param config 配置选项
   */
  constructor(
    sender?: (content: string) => Promise<boolean>,
    config?: Partial<MessageQueueConfig>
  ) {
    this.config = {
      ...DEFAULT_CONFIG,
      ...config,
    }

    // 初始化IndexedDB数据库
    this.db = new MessageQueueDatabase(this.config.dbName)

    // 设置发送函数
    if (sender) {
      this.sender = sender
    } else {
      // 默认发送函数，会尝试使用sendHandler
      this.sender = async (content: string): Promise<boolean> => {
        if (this.sendHandler) {
          await this.sendHandler(content)
          return true
        }
        throw new Error('发送处理器未设置')
      }
    }
  }

  /**
   * 生成唯一消息ID
   *
   * @returns 唯一消息ID字符串
   */
  private generateId(): string {
    return `msg_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
  }

  /**
   * 计算指数退避延迟
   *
   * 公式：min(baseDelay * (backoffFactor ^ retries), maxDelay)
   *
   * @param retries 当前重试次数
   * @returns 延迟时间（毫秒）
   */
  private calculateBackoffDelay(retries: number): number {
    const delay =
      this.config.baseRetryDelay *
      Math.pow(this.config.retryBackoffFactor, retries)
    return Math.min(delay, this.config.maxRetryDelay)
  }

  /**
   * 将消息添加到队列
   *
   * @param content 消息内容（JSON字符串）
   * @param options 发送选项
   * @returns 消息ID
   */
  async enqueue(content: string, options?: SendOptions): Promise<string> {
    const messageId = this.generateId()
    const now = Date.now()

    const message: QueuedMessage = {
      id: messageId,
      content,
      status: MessageStatus.PENDING,
      retries: 0,
      maxRetries: options?.maxRetries ?? this.config.maxRetries,
      timestamp: now,
      priority: options?.priority ?? 1,
    }

    // 保存回调
    if (options) {
      this.callbacks.set(messageId, options)
    }

    // 持久化到IndexedDB
    if (this.config.enablePersistence) {
      await this.db.messages.add(message)
    }

    console.log(`[MessageQueue] 消息已入队: ${messageId}`)

    // 触发队列处理
    this.scheduleProcess()

    return messageId
  }

  /**
   * 设置发送消息的实现
   *
   * @param handler 发送处理器
   */
  setSendHandler(handler: (content: string) => Promise<void>): void {
    this.sendHandler = handler
    // 包装sender以使用新的handler
    this.sender = async (content: string): Promise<boolean> => {
      await handler(content)
      return true
    }
  }

  /**
   * 暂停队列处理
   */
  pause(): void {
    console.log('[EnhancedMessageQueue] 队列已暂停')
    this.paused = true
  }

  /**
   * 恢复队列处理
   */
  resume(): void {
    console.log('[EnhancedMessageQueue] 队列已恢复')
    this.paused = false
    this.stopped = false
    this.scheduleProcess()
  }

  /**
   * 获取队列状态
   *
   * @returns 队列状态信息
   */
  getStatus() {
    return {
      size: this.callbacks.size,
      paused: this.paused,
      stopped: this.stopped,
      isProcessing: this.isProcessing,
    }
  }

  /**
   * 从队列中取出并处理一条消息
   */
  private async processNextMessage(): Promise<void> {
    // 如果已停止、已暂停或正在处理，跳过
    if (this.stopped || this.paused || this.isProcessing) {
      if (this.paused) {
        console.log('[EnhancedMessageQueue] 队列已暂停，跳过处理')
      }
      return
    }

    // 从队列中获取下一条待发送消息
    // 条件：状态为PENDING，且（无重试时间 或 重试时间已到）
    const now = Date.now()
    const message = await this.db.messages
      .where('status')
      .equals(MessageStatus.PENDING)
      .and(msg => !msg.nextRetryTime || msg.nextRetryTime <= now)
      .sortBy('priority')
      .then(
        msgs =>
          msgs.sort((a, b) => {
            // 优先级相同按时间排序（FIFO）
            if (a.priority === b.priority) {
              return a.timestamp - b.timestamp
            }
            return b.priority - a.priority // 高优先级在前
          })[0]
      )

    if (!message) {
      // 没有待处理消息
      this.isProcessing = false
      return
    }

    this.isProcessing = true

    try {
      // 更新状态为发送中
      message.status = MessageStatus.SENDING
      await this.db.messages.put(message)

      console.log(
        `[MessageQueue] 发送消息: ${message.id}, 重试次数: ${message.retries}`
      )

      // 调用发送函数
      const success = await this.sender(message.content)

      if (success) {
        // 发送成功
        await this.handleSendSuccess(message)
      } else {
        // 发送失败
        await this.handleSendFailure(message, new Error('发送失败'))
      }
    } catch (error) {
      // 发送异常
      await this.handleSendFailure(message, error as Error)
    } finally {
      this.isProcessing = false
      // 继续处理下一条消息
      this.scheduleProcess()
    }
  }

  /**
   * 处理发送成功
   *
   * @param message 已发送的消息
   */
  private async handleSendSuccess(message: QueuedMessage): Promise<void> {
    // 更新状态
    message.status = MessageStatus.SENT
    await this.db.messages.put(message)

    console.log(`[MessageQueue] 消息发送成功: ${message.id}`)

    // 调用成功回调
    const options = this.callbacks.get(message.id)
    if (options?.onSent) {
      try {
        options.onSent(message.id)
      } catch (error) {
        console.error('[MessageQueue] onSent回调错误:', error)
      }
    }

    // 清理回调
    this.callbacks.delete(message.id)
  }

  /**
   * 处理发送失败
   *
   * @param message 发送失败的消息
   * @param error 错误信息
   */
  private async handleSendFailure(
    message: QueuedMessage,
    error: Error
  ): Promise<void> {
    message.retries++

    console.warn(
      `[MessageQueue] 消息发送失败: ${message.id}, ` +
        `重试次数: ${message.retries}/${message.maxRetries}`
    )

    if (message.retries >= message.maxRetries) {
      // 达到最大重试次数，标记为失败
      message.status = MessageStatus.FAILED
      await this.db.messages.put(message)

      console.error(`[MessageQueue] 消息最终失败: ${message.id}`)

      // 调用失败回调
      const options = this.callbacks.get(message.id)
      if (options?.onFailed) {
        try {
          options.onFailed(message.id, error)
        } catch (err) {
          console.error('[MessageQueue] onFailed回调错误:', err)
        }
      }

      // 清理回调
      this.callbacks.delete(message.id)
    } else {
      // 计算下次重试时间（指数退避）
      const delay = this.calculateBackoffDelay(message.retries)
      message.nextRetryTime = Date.now() + delay
      message.status = MessageStatus.PENDING

      await this.db.messages.put(message)

      console.log(`[MessageQueue] 消息将在 ${delay}ms 后重试: ${message.id}`)

      // 调度下次处理
      this.scheduleProcess(delay)
    }
  }

  /**
   * 调度队列处理
   *
   * @param delay 延迟时间（毫秒），默认立即处理
   */
  private scheduleProcess(delay: number = 0): void {
    // 如果已停止，不再安排新的处理
    if (this.stopped) {
      return
    }

    // 清除现有定时器
    if (this.processTimer) {
      clearTimeout(this.processTimer)
      this.processTimer = null
    }

    // 设置新的定时器，并添加错误处理
    this.processTimer = setTimeout(() => {
      this.processNextMessage().catch(error => {
        console.error('[MessageQueue] 处理消息时出错:', error)
      })
    }, delay)
  }

  /**
   * 开始处理队列（通常在WebSocket重连后调用）
   */
  async start(): Promise<void> {
    console.log('[MessageQueue] 开始处理队列')
    this.stopped = false
    this.scheduleProcess()
  }

  /**
   * 停止处理队列
   */
  stop(): void {
    console.log('[MessageQueue] 停止处理队列')
    this.stopped = true
    if (this.processTimer) {
      clearTimeout(this.processTimer)
      this.processTimer = null
    }
    this.isProcessing = false
  }

  /**
   * 清空队列（删除所有消息）
   */
  async clear(): Promise<void> {
    this.stop()
    await this.db.messages.clear()
    this.callbacks.clear()
    console.log('[MessageQueue] 队列已清空')
  }

  /**
   * 获取队列统计信息
   *
   * @returns 各状态消息数量统计
   */
  async getStats(): Promise<{
    pending: number
    sending: number
    sent: number
    failed: number
    total: number
  }> {
    const [pending, sending, sent, failed] = await Promise.all([
      this.db.messages.where('status').equals(MessageStatus.PENDING).count(),
      this.db.messages.where('status').equals(MessageStatus.SENDING).count(),
      this.db.messages.where('status').equals(MessageStatus.SENT).count(),
      this.db.messages.where('status').equals(MessageStatus.FAILED).count(),
    ])

    return {
      pending,
      sending,
      sent,
      failed,
      total: pending + sending + sent + failed,
    }
  }

  /**
   * 重新入队失败的消息（用于手动恢复）
   *
   * @returns 重新入队的消息数量
   */
  async retryFailedMessages(): Promise<number> {
    const failedMessages = await this.db.messages
      .where('status')
      .equals(MessageStatus.FAILED)
      .toArray()

    for (const message of failedMessages) {
      message.status = MessageStatus.PENDING
      message.retries = 0
      message.nextRetryTime = undefined
      await this.db.messages.put(message)
    }

    console.log(`[MessageQueue] 已重新入队 ${failedMessages.length} 条失败消息`)

    // 触发处理
    this.scheduleProcess()

    return failedMessages.length
  }

  /**
   * 清理旧消息（删除超过指定时间的已发送消息）
   *
   * @param maxAge 最大保留时间（毫秒），默认24小时
   * @returns 清理的消息数量
   */
  async cleanup(maxAge: number = 24 * 60 * 60 * 1000): Promise<number> {
    const cutoffTime = Date.now() - maxAge

    const count = await this.db.messages
      .where('status')
      .equals(MessageStatus.SENT)
      .and(msg => msg.timestamp < cutoffTime)
      .delete()

    if (count > 0) {
      console.log(`[MessageQueue] 已清理 ${count} 条旧消息`)
    }

    return count
  }

  /**
   * 关闭队列（释放资源）
   */
  async close(): Promise<void> {
    this.stop()
    await this.db.close()
    console.log('[MessageQueue] 队列已关闭')
  }
}
