/**
 * WebSocket 连接池管理器
 *
 * 管理多个并发的 WebSocket 连接（每个会话/线程一个），
 * 支持会话切换时后台管道继续独立流式输出。
 *
 * 核心设计：
 * - 每个 threadId 对应一个独立的 WebSocketService 实例
 * - 所有连接的事件汇聚到统一的事件总线，携带 _threadId 标识
 * - 消费方（streamingEventService 等）根据 _threadId 路由事件到正确的会话
 * - 会话切换时旧连接保持活跃，新连接按需创建
 */

import { loggers } from '@/utils/logger'
import { WebSocketService, WebSocketStatus, type WebSocketStatusType } from './WebSocketService'
import type { EventHandler } from './eventHandlers'
import type { ApprovalDecisionType } from '@/constants/websocket'

interface PooledConnection {
  ws: WebSocketService
  threadId: string
}

const logger = loggers.websocket

export class WebSocketConnectionPool {
  private connections: Map<string, PooledConnection> = new Map()
  private globalHandlers: Map<string, Set<EventHandler>> = new Map()
  private _activeThreadId: string | null = null

  constructor() {}

  /**
   * 建立或复用指定线程的 WebSocket 连接
   */
  connect(threadId: string, token: string): void {
    const existing = this.connections.get(threadId)
    if (existing && existing.ws.isConnected()) {
      logger.debug(`[Pool] 连接已存在，跳过 | threadId: ${threadId}`)
      return
    }

    if (existing) {
      existing.ws.disconnect()
      this.connections.delete(threadId)
    }

    const ws = new WebSocketService()

    ws.setOnAnyMessage((type: string, data: unknown) => {
      this.emitGlobal(type, { ...(data as Record<string, unknown>), _threadId: threadId })
    })

    ws.connect(threadId, token)
    this.connections.set(threadId, { ws, threadId })
    this._activeThreadId = threadId

    logger.info(`[Pool] 连接已建立 | threadId: ${threadId} | 总连接数: ${this.connections.size}`)
  }

  /**
   * 断开指定线程的连接
   */
  disconnect(threadId: string): void {
    const conn = this.connections.get(threadId)
    if (!conn) return

    conn.ws.setOnAnyMessage(undefined)
    conn.ws.disconnect()
    this.connections.delete(threadId)

    if (this._activeThreadId === threadId) {
      this._activeThreadId = null
    }

    logger.info(`[Pool] 连接已断开 | threadId: ${threadId} | 剩余连接数: ${this.connections.size}`)
  }

  /**
   * 断开所有连接
   */
  disconnectAll(): void {
    for (const [threadId, conn] of this.connections) {
      conn.ws.setOnAnyMessage(undefined)
      conn.ws.disconnect()
    }
    this.connections.clear()
    this._activeThreadId = null
    logger.info('[Pool] 所有连接已断开')
  }

  /**
   * 设置当前活跃线程（用于发送消息的默认目标）
   */
  setActiveThread(threadId: string | null): void {
    this._activeThreadId = threadId
  }

  /**
   * 获取当前活跃线程 ID
   */
  getActiveThread(): string | null {
    return this._activeThreadId
  }

  /**
   * 获取指定线程的连接实例
   */
  getConnection(threadId: string): WebSocketService | undefined {
    return this.connections.get(threadId)?.ws
  }

  /**
   * 检查指定线程是否已连接
   */
  isConnected(threadId: string): boolean {
    return this.connections.get(threadId)?.ws.isConnected() ?? false
  }

  /**
   * 检查是否存在任何活跃连接
   */
  hasAnyConnection(): boolean {
    for (const [, conn] of this.connections) {
      if (conn.ws.isConnected()) return true
    }
    return false
  }

  /**
   * 获取整体连接状态（任意连接已连接则返回 connected）
   */
  getStatus(): WebSocketStatusType {
    for (const [, conn] of this.connections) {
      if (conn.ws.isConnected()) return WebSocketStatus.CONNECTED
    }
    for (const [, conn] of this.connections) {
      const status = conn.ws.getStatus()
      if (status === WebSocketStatus.CONNECTING || status === WebSocketStatus.RECONNECTING) {
        return status
      }
    }
    if (this.connections.size > 0) return WebSocketStatus.DISCONNECTED
    return WebSocketStatus.DISCONNECTED
  }

  /**
   * 获取指定线程的连接状态
   */
  getThreadStatus(threadId: string): WebSocketStatusType {
    return this.connections.get(threadId)?.ws.getStatus() ?? WebSocketStatus.DISCONNECTED
  }

  subscribe(event: string, handler: EventHandler): void {
    if (!this.globalHandlers.has(event)) {
      this.globalHandlers.set(event, new Set())
    }
    this.globalHandlers.get(event)!.add(handler)
  }

  unsubscribe(event: string, handler: EventHandler): void {
    const handlers = this.globalHandlers.get(event)
    if (handlers) {
      handlers.delete(handler)
      if (handlers.size === 0) {
        this.globalHandlers.delete(event)
      }
    }
  }

  private emitGlobal(event: string, data: unknown): void {
    const handlers = this.globalHandlers.get(event)
    if (handlers) {
      for (const handler of handlers) {
        try {
          handler(data)
        } catch (error) {
          logger.error(`[Pool] 全局事件处理器执行失败 (${event}):`, error)
        }
      }
    }
  }

  /**
   * 向指定线程发送用户输入
   */
  async sendUserInput(
    threadId: string,
    content: string,
    attachments?: Array<{ type: string; url: string; name: string }>,
    enableThinking?: boolean,
    pipelineId?: string,
  ): Promise<{ messageId: string } | null> {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) {
      logger.warn(`[Pool] 发送失败：未找到连接 | threadId: ${threadId}`)
      return null
    }
    return ws.sendUserInput(content, attachments, enableThinking, pipelineId)
  }

  /**
   * 向指定线程发送取消请求
   */
  async sendCancel(threadId: string, reason?: string): Promise<boolean> {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) {
      logger.warn(`[Pool] 取消失败：未找到连接 | threadId: ${threadId}`)
      return false
    }
    return ws.sendCancel(reason)
  }

  /**
   * 向指定线程发送审批决策
   */
  async sendApproval(
    threadId: string,
    decision: ApprovalDecisionType,
    reason?: string,
    modifications?: Record<string, unknown>,
  ): Promise<boolean> {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) return false
    return ws.sendApproval(decision, reason, modifications)
  }

  /**
   * 向指定线程发送用户输入响应
   */
  async sendUserInputResponse(threadId: string, executionId: string, input: string): Promise<boolean> {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) return false
    return ws.sendUserInputResponse(executionId, input)
  }

  /**
   * 向指定线程发送交互响应
   */
  async sendInteractionResponse(
    threadId: string,
    params: {
      requestId: string
      responseType: 'approved' | 'denied' | 'answered'
      selectedOption?: string
      answers?: string[]
      feedback?: string
    },
  ): Promise<boolean> {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) return false
    return ws.sendInteractionResponse(params)
  }

  /**
   * 获取指定线程的性能统计
   */
  async getPerformanceStats(threadId: string) {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) return null
    return ws.getPerformanceStats()
  }

  /**
   * 获取指定线程的网络质量
   */
  getNetworkQuality(threadId: string) {
    const ws = this.connections.get(threadId)?.ws
    if (!ws) return 'unknown'
    return ws.getNetworkQuality()
  }

  /**
   * 获取所有活跃连接的线程 ID 列表
   */
  getActiveThreadIds(): string[] {
    const active: string[] = []
    for (const [threadId, conn] of this.connections) {
      if (conn.ws.isConnected()) {
        active.push(threadId)
      }
    }
    return active
  }

  /**
   * 获取连接池大小
   */
  get size(): number {
    return this.connections.size
  }
}

export const wsPool = new WebSocketConnectionPool()
export default wsPool
