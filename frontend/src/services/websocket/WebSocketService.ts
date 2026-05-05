/**
 * WebSocket 服务（简化版）
 *
 * 提供 WebSocket 连接管理、事件订阅和自动重连功能，
 * 对应后端端点：/ws/chat/{thread_id}?token={token}
 *
 * 后端新协议：data 在顶层，不再嵌套 data.data
 *
 * 暴露接口：
 * - connect(threadId, token) - 连接 WebSocket 服务器
 * - disconnect() - 断开 WebSocket 连接
 * - subscribe(event, handler) - 订阅事件
 * - unsubscribe(event, handler) - 取消订阅事件
 * - send(message) - 发送通用 WebSocket 消息
 * - sendUserInput(content, attachments?, enableThinking?, parentRecordId?) - 发送用户输入
 * - sendApproval(decision, reason?, modifications?) - 发送审批决策
 * - sendHeartbeat() - 发送心跳
 * - sendCancel(reason?) - 发送取消请求
 * - sendUserInputResponse(executionId, input) - 发送用户输入响应
 * - getStatus() / isConnected() / getThreadId() / getToken() - 状态查询
 * - pauseQueue() / resumeQueue() / clearQueue() / getQueueStatus() - 消息队列管理
 * - getPerformanceStats() / getNetworkQuality() / resetPerformanceStats() / exportPerformanceData() - 性能监控
 */

import {
  DEFAULT_RETRY_POLICY,
  WS_ACK_MAX_RETRIES,
  WS_ACK_REQUIRED_EVENTS,
  WS_ACK_TIMEOUT,
  WS_CLIENT_MESSAGES,
  WS_HEARTBEAT_CONFIG,
  WS_RECONNECT_CONFIG,
  WS_SERVER_EVENTS,
  buildWebSocketUrl,
} from '@/constants/websocket'
import { getWebSocketMonitor } from '@/lib/monitoring'
import apiClient from '@/services/api/client'
import { tokenManager } from '@/stores/tokenManager'
import {
  MESSAGE_CONFIG,
  MessageSendStrategy,
  MessageTypes,
  createStandardMessage,
} from '@/types/websocket'
import { loggers } from '@/utils/logger'
import { EnhancedMessageQueue, MessagePriority } from './EnhancedMessageQueue'
import { type WebSocketErrorHandler, createWebSocketErrorHandler } from './errorHandler'
import { HeartbeatManager } from './HeartbeatManager'
import type { MessagePriorityType } from './EnhancedMessageQueue'
import type { EventHandler } from './eventHandlers'
import type { HeartbeatCallbacks } from './HeartbeatManager'
import type {
  ApprovalDecisionType,
  ApprovalMessage,
  MessageAckMessage,
  RequestMissedMessage,
  UserInputResponseMessage,
  WebSocketClientMessage,
} from '@/constants/websocket'
import type { HeartbeatMessage, StandardMessage, UserInputMessage } from '@/types/websocket'

/**
 * 内部事件常量（用于连接生命周期事件）
 */
const INTERNAL_EVENTS = {
  /** 连接建立 */
  CONNECT: 'connect',
  /** 连接断开 */
  DISCONNECT: 'disconnect',
  /** 连接错误 */
  ERROR: 'error',
} as const

/**
 * WebSocket 连接状态
 */
export const WebSocketStatus = {
  /** 未连接 */
  DISCONNECTED: 'disconnected',
  /** 连接中 */
  CONNECTING: 'connecting',
  /** 已连接 */
  CONNECTED: 'connected',
  /** 重连中 */
  RECONNECTING: 'reconnecting',
  /** 连接失败 */
  FAILED: 'failed',
} as const

/** WebSocket 状态类型 */
export type WebSocketStatusType =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'failed'

/**
 * WebSocket 服务类
 *
 * 提供与后端 WebSocket 端点的连接管理，支持：
 * - 基于 thread_id 的连接
 * - JWT 令牌认证
 * - 自动重连（指数退避）
 * - 事件订阅和消息发送
 */
export class WebSocketService {
  /** WebSocket 实例 */
  private ws: WebSocket | null = null

  /** 连接状态 */
  private status: WebSocketStatusType = WebSocketStatus.DISCONNECTED

  /** 认证令牌 */
  private token: string | null = null

  /** 当前线程 ID */
  private threadId: string | null = null

  /** 事件处理器映射 */
  private eventHandlers: Map<string, Set<EventHandler>> = new Map()

  /** 重连尝试次数 */
  private reconnectAttempts = 0

  /** 重连定时器 */
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  /** 心跳定时器 */
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null

  /** 心跳超时定时器 */
  private heartbeatTimeoutTimer: ReturnType<typeof setTimeout> | null = null

  /** 是否手动断开连接 */
  private manualDisconnect = false

  /** 消息队列 */
  private messageQueue: EnhancedMessageQueue

  /** 错误处理器 */
  private errorHandler: WebSocketErrorHandler

  /** 性能监控器 */
  private monitor = getWebSocketMonitor()

  /** 心跳管理器 */
  private heartbeatManager: HeartbeatManager

  /** 最后收到的消息 request_id（用于重连后请求遗漏消息） */
  private lastReceivedRequestId: string = ''

  /** 协议版本（从服务端 connection_confirmation 获取） */
  private negotiatedVersion: string = ''

  /** 等待 ACK 的消息追踪 Map: request_id -> { timer, retries } */
  private pendingAckTimers: Map<
    string,
    { timer: ReturnType<typeof setTimeout>; retries: number }
  > = new Map()

  constructor() {
    // 初始化消息队列（IndexedDB 持久化 + 指数退避重试）
    this.messageQueue = new EnhancedMessageQueue(undefined, {
      dbName: 'WebSocketMessageQueue',
      maxRetries: DEFAULT_RETRY_POLICY.maxRetries,
      baseRetryDelay: 1000,
      maxRetryDelay: 10000,
      retryBackoffFactor: 2,
      enablePersistence: true,
    })

    // 初始化错误处理器
    this.errorHandler = createWebSocketErrorHandler()

    // 初始化性能监控
    this.monitor = getWebSocketMonitor()

    // 初始化心跳管理器
    this.heartbeatManager = new HeartbeatManager()
    this.setupHeartbeatCallbacks()

    // 设置消息队列的发送处理器
    this.messageQueue.setSendHandler(async (message: string) => {
      await this.sendMessageDirect(message as unknown as WebSocketClientMessage)
    })
  }

  /**
   * 连接 WebSocket 服务器
   *
   * @param threadId 线程 ID
   * @param token JWT 访问令牌
   */
  connect(threadId: string, token: string): void {
    // 防重复连接：参数相同且已连接时直接返回
    if (
      this.status === WebSocketStatus.CONNECTED &&
      this.threadId === threadId &&
      this.token === token &&
      this.ws &&
      this.ws.readyState === WebSocket.OPEN
    ) {
      return
    }

    // 已有连接时先断开
    if (this.ws && this.status !== WebSocketStatus.DISCONNECTED) {
      this.disconnect()
    }

    this.threadId = threadId
    this.token = token
    this.manualDisconnect = false
    this.status = WebSocketStatus.CONNECTING

    try {
      const wsUrl = buildWebSocketUrl(this.threadId, this.token)
      loggers.websocket.info('正在连接:', wsUrl.replace(/token=[^&]+/, 'token=***'))
      this.ws = new WebSocket(wsUrl)

      this.ws.onopen = this.handleOpen.bind(this)
      this.ws.onmessage = (event: MessageEvent) => {
        this.handleMessage(event).catch((error) => {
          loggers.websocket.error('处理消息时发生错误:', error)
        })
      }
      this.ws.onerror = this.handleError.bind(this)
      this.ws.onclose = this.handleClose.bind(this)
    } catch (error) {
      loggers.websocket.error('连接失败:', error)
      this.status = WebSocketStatus.FAILED
      this.handleReconnect()
    }
  }

  /**
   * 获取当前连接的线程 ID
   */
  getThreadId(): string | null {
    return this.threadId
  }

  /**
   * 获取当前连接的令牌
   */
  getToken(): string | null {
    return this.token
  }

  /**
   * 设置心跳回调
   */
  private setupHeartbeatCallbacks(): void {
    const callbacks: HeartbeatCallbacks = {
      onSendHeartbeat: async () => {
        await this.sendHeartbeat()
      },
      onHeartbeatResponse: (rtt: number) => {
        this.monitor.recordHeartbeatLatency(rtt)
      },
      onHeartbeatTimeout: () => {
        loggers.heartbeat.warn('心跳超时')
        this.monitor.recordError('heartbeat_timeout', '心跳超时')
      },
      onNetworkQualityChange: (quality: string) => {
        loggers.heartbeat.info(`网络质量变化: ${quality}`)
        this.emit('network_quality_change', { quality })
      },
    }

    this.heartbeatManager.setCallbacks(callbacks)
  }

  /**
   * 断开 WebSocket 连接
   */
  disconnect(): void {
    this.manualDisconnect = true
    this.clearTimers()

    // 清除所有 ACK 待确认定时器
    this.clearAckTimers()

    // 停止心跳管理器
    this.heartbeatManager.stop()

    // 记录连接结束
    this.monitor.recordConnectionEnd()

    if (this.ws) {
      // 移除事件监听器
      this.ws.onopen = null
      this.ws.onmessage = null
      this.ws.onerror = null
      this.ws.onclose = null

      // 关闭连接
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.close()
      }

      this.ws = null
    }

    this.status = WebSocketStatus.DISCONNECTED
    this.reconnectAttempts = 0

    // 触发断开连接事件
    this.emit(INTERNAL_EVENTS.DISCONNECT, { manual: true })
  }

  /**
   * 订阅事件
   *
   * @param event 事件类型
   * @param handler 事件处理器
   */
  subscribe(event: string, handler: EventHandler): void {
    if (!this.eventHandlers.has(event)) {
      this.eventHandlers.set(event, new Set())
    }
    this.eventHandlers.get(event)!.add(handler)
  }

  /**
   * 取消订阅事件
   *
   * @param event 事件类型
   * @param handler 事件处理器
   */
  unsubscribe(event: string, handler: EventHandler): void {
    const handlers = this.eventHandlers.get(event)
    if (handlers) {
      handlers.delete(handler)
      if (handlers.size === 0) {
        this.eventHandlers.delete(event)
      }
    }
  }

  /**
   * 直接发送消息（不经过队列）
   *
   * @param message 客户端消息对象或已序列化的字符串
   */
  private async sendMessageDirect(message: WebSocketClientMessage | string): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      const errorMsg = 'WebSocket 连接未建立'
      loggers.websocket.error(errorMsg, {
        hasWs: !!this.ws,
        readyState: this.ws?.readyState,
        threadId: this.threadId,
      })
      throw new Error(errorMsg)
    }

    try {
      const messageStr = typeof message === 'string' ? message : JSON.stringify(message)
      const messageType = typeof message === 'string' ? 'unknown' : message.type || 'unknown'

      this.monitor.recordMessageSent(messageType, messageStr.length)
      loggers.websocket.debug(
        '发送消息 |',
        messageStr.substring(0, 200) + (messageStr.length > 200 ? '...' : ''),
      )
      this.ws.send(messageStr)
    } catch (error) {
      loggers.websocket.error('发送消息失败:', error)
      this.monitor.recordMessageFailed()
      throw new Error(`发送消息失败: ${error}`)
    }
  }

  /**
   * 发送通用 WebSocket 消息（公共接口）
   *
   * @param message 消息对象或已序列化的 JSON 字符串
   * @returns 是否发送成功
   */
  async send(message: Record<string, unknown> | string): Promise<boolean> {
    try {
      const messageStr = typeof message === 'string' ? message : JSON.stringify(message)
      await this.sendMessageDirect(messageStr)
      return true
    } catch (error) {
      loggers.websocket.error('send 方法发送失败:', error)
      return false
    }
  }

  /**
   * 发送标准格式的 WebSocket 消息（根据策略选择发送方式）
   *
   * @param type 消息类型
   * @param data 消息数据
   * @param options 可选参数（messageId / timestamp）
   * @returns 是否发送成功
   */
  private async sendStandardMessage<T extends StandardMessage>(
    type: T['type'],
    data: T['data'],
    options?: {
      messageId?: string
      timestamp?: string
    },
  ): Promise<boolean> {
    if (!this.threadId) {
      loggers.websocket.error('无法发送消息：缺少线程 ID')
      return false
    }

    const standardMessage = createStandardMessage<T>(type, this.threadId, data, options)
    const config = MESSAGE_CONFIG[type]

    if (!config) {
      loggers.websocket.warn(`未找到消息类型 "${type}" 的配置，使用队列发送`)
      try {
        await this.messageQueue.enqueue(JSON.stringify(standardMessage), {
          priority: MessagePriority.NORMAL,
          onSent: () => {
            loggers.messageQueue.debug('标准消息发送成功:', type)
          },
          onFailed: (error) => {
            loggers.messageQueue.error('标准消息发送失败:', type, error)
          },
        })
        return true
      } catch (error) {
        loggers.messageQueue.error('标准消息入队失败:', error)
        return false
      }
    }

    // 根据配置选择发送策略
    if (config.strategy === MessageSendStrategy.DIRECT) {
      try {
        await this.sendMessageDirect(standardMessage as WebSocketClientMessage)
        return true
      } catch (error) {
        loggers.websocket.error(`直接发送消息失败 (${type}):`, error)
        return false
      }
    } else {
      try {
        await this.messageQueue.enqueue(JSON.stringify(standardMessage), {
          priority: config.priority as MessagePriorityType,
          onSent: () => {
            loggers.messageQueue.debug('队列消息发送成功:', type)
          },
          onFailed: (error) => {
            loggers.messageQueue.error('队列消息发送失败:', type, error)
          },
        })
        return true
      } catch (error) {
        loggers.messageQueue.error('消息入队失败:', error)
        return false
      }
    }
  }

  /**
   * 通过队列发送消息
   *
   * @param message 客户端消息
   * @param priority 消息优先级
   */
  private async sendMessage(
    message: WebSocketClientMessage,
    priority: MessagePriorityType = MessagePriority.NORMAL,
  ): Promise<boolean> {
    try {
      await this.messageQueue.enqueue(JSON.stringify(message), {
        priority,
        onSent: () => {
          loggers.messageQueue.debug('消息发送成功:', message.type)
        },
        onFailed: (error) => {
          loggers.messageQueue.error('消息发送失败:', message.type, error)
        },
      })
      return true
    } catch (error) {
      loggers.messageQueue.error('消息入队失败:', error)
      return false
    }
  }

  /**
   * 发送用户输入消息
   *
   * @param content 用户输入内容
   * @param attachments 文件附件列表
   * @param enableThinking 是否启用思考模式
   * @param parentRecordId 父执行记录 ID
   * @returns 生成的消息 ID，失败返回 null
   */
  async sendUserInput(
    content: string,
    attachments?: Array<{
      type: string
      url: string
      name: string
    }>,
    enableThinking?: boolean,
    parentRecordId?: string,
  ): Promise<{ messageId: string } | null> {
    const messageId = crypto.randomUUID()

    loggers.websocket.info(
      '发送用户输入:',
      content.substring(0, 50) + (content.length > 50 ? '...' : ''),
      '思考模式:',
      enableThinking,
      'parentRecordId:',
      parentRecordId,
    )

    const success = await this.sendStandardMessage<UserInputMessage>(
      MessageTypes.USER_INPUT,
      {
        content,
        ...(attachments && { attachments }),
        ...(enableThinking !== undefined && { enable_thinking: enableThinking }),
        ...(parentRecordId && { parent_record_id: parentRecordId }),
      },
      { messageId },
    )

    return success ? { messageId } : null
  }

  /**
   * 发送审批决策消息
   *
   * @param decision 审批决策（approve/reject/modify）
   * @param reason 决策原因
   * @param modifications 修改内容（仅 decision 为 modify 时使用）
   * @returns 是否发送成功
   */
  async sendApproval(
    decision: ApprovalDecisionType,
    reason?: string,
    modifications?: Record<string, unknown>,
  ): Promise<boolean> {
    const message: ApprovalMessage = {
      type: WS_CLIENT_MESSAGES.APPROVAL,
      decision,
    }

    if (reason !== undefined) {
      message.reason = reason
    }

    if (modifications !== undefined && decision === 'modify') {
      message.modifications = modifications
    }

    loggers.websocket.info('发送审批决策:', decision, reason ? `原因: ${reason}` : '')
    return await this.sendMessage(message, MessagePriority.HIGH)
  }

  /**
   * 发送心跳消息（直接发送，不经过队列）
   *
   * @returns 是否发送成功
   */
  async sendHeartbeat(): Promise<boolean> {
    if (!this.threadId) {
      loggers.heartbeat.error('无法发送心跳：缺少线程 ID')
      return false
    }

    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      loggers.heartbeat.warn('WebSocket 未连接，无法发送心跳')
      return false
    }

    try {
      const heartbeatMessage = createStandardMessage<HeartbeatMessage>(
        MessageTypes.HEARTBEAT,
        this.threadId,
        { client_timestamp: new Date().toISOString() },
      )
      await this.sendMessageDirect(heartbeatMessage as unknown as WebSocketClientMessage)
      return true
    } catch (error) {
      loggers.heartbeat.error('心跳消息发送失败:', error)
      return false
    }
  }

  /**
   * 发送客户端能力上报消息
   *
   * WebSocket 连接建立后，主动向服务端上报客户端的能力信息，
   * 包括支持的渲染空间、停靠模式、浮动位置和必需组件等。
   * 该消息通过直接发送（不经过队列），确保服务端尽早获知客户端能力。
   */
  private sendClientCapabilities(): void {
    const capabilitiesMessage = {
      type: 'client_capabilities',
      data: {
        client_type: 'web',
        rendering_spaces: ['chat', 'workspace', 'floating', 'dock', 'fullscreen'],
        dock: true,
        floating_position: ['center', 'bottom_right', 'top_right'],
        required_widgets: [
          'form', 'chart', 'gallery', 'table',
          'progress', 'code_block', 'status_card', 'decision',
        ],
      },
    }

    this.sendMessageDirect(capabilitiesMessage as unknown as WebSocketClientMessage)
      .then(() => {
        loggers.websocket.info('客户端能力上报已发送')
      })
      .catch((error) => {
        loggers.websocket.error('客户端能力上报发送失败:', error)
      })
  }

  /**
   * 发送取消请求（直接发送，不经过队列）
   *
   * @param reason 取消原因
   * @returns 是否发送成功
   */
  async sendCancel(reason?: string): Promise<boolean> {
    loggers.websocket.info('发送取消请求:', reason || '用户手动停止')

    if (!this.threadId) {
      loggers.websocket.error('无法发送取消请求：缺少线程 ID')
      return false
    }

    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      loggers.websocket.warn('WebSocket 未连接，无法发送取消请求')
      return false
    }

    try {
      const stopMessage = {
        type: 'stop_generation',
        thread_id: this.threadId,
        ...(reason && { reason }),
      }
      await this.sendMessageDirect(stopMessage as WebSocketClientMessage)
      return true
    } catch (error) {
      loggers.websocket.error('取消请求发送失败:', error)
      return false
    }
  }

  /**
   * 发送用户输入响应消息（响应子 Agent 的输入请求）
   *
   * @param executionId 执行 ID
   * @param input 用户输入内容
   * @returns 是否发送成功
   */
  async sendUserInputResponse(executionId: string, input: string): Promise<boolean> {
    const message: UserInputResponseMessage = {
      type: WS_CLIENT_MESSAGES.USER_INPUT_RESPONSE,
      execution_id: executionId,
      response: input,
    }

    loggers.websocket.info(
      '发送用户输入响应:',
      executionId,
      input.substring(0, 50) + (input.length > 50 ? '...' : ''),
    )
    return await this.sendMessage(message, MessagePriority.HIGH)
  }

  /**
   * 发送交互响应消息（响应 human_interaction 工具的请求）
   *
   * @param params 响应参数
   * @returns 是否发送成功
   */
  async sendInteractionResponse(params: {
    requestId: string
    responseType: 'approved' | 'denied' | 'answered'
    selectedOption?: string
    answers?: string[]
    feedback?: string
  }): Promise<boolean> {
    const message = {
      type: 'interaction_response',
      data: {
        request_id: params.requestId,
        response_type: params.responseType,
        selected_option: params.selectedOption,
        answers: params.answers,
        feedback: params.feedback,
      },
    }

    loggers.websocket.info(
      '发送交互响应:',
      params.requestId,
      params.responseType,
    )
    return await this.sendMessage(
      message as unknown as WebSocketClientMessage,
      MessagePriority.HIGH,
    )
  }

  /**
   * 获取当前连接状态
   */
  getStatus(): WebSocketStatusType {
    return this.status
  }

  /**
   * 检查是否已连接
   */
  isConnected(): boolean {
    return this.status === WebSocketStatus.CONNECTED
  }

  /**
   * 处理连接建立
   *
   * 连接建立后：
   * 1. 更新连接状态
   * 2. 发送客户端能力上报
   * 3. 恢复消息队列
   * 4. 启动心跳
   * 5. 如果是重连，请求遗漏消息（恢复状态）
   */
  private handleOpen(): void {
    const wasReconnecting = this.reconnectAttempts > 0
    loggers.websocket.info('连接已建立', wasReconnecting ? '(重连)' : '(首次)')
    this.status = WebSocketStatus.CONNECTED
    this.reconnectAttempts = 0

    // 记录连接开始监控数据
    this.monitor.recordConnectionStart()

    // 发送客户端能力上报（连接成功后立即发送，确保服务端尽早获知客户端能力）
    this.sendClientCapabilities()

    // 恢复消息队列处理
    loggers.messageQueue.debug('恢复消息队列...')
    this.messageQueue.resume()

    // 启动心跳
    this.startHeartbeat()

    // 重连后请求遗漏的消息
    if (wasReconnecting && this.lastReceivedRequestId) {
      loggers.websocket.info(
        '重连恢复：请求遗漏消息, last_request_id:',
        this.lastReceivedRequestId,
      )
      // 稍微延迟请求，等待 connection_confirmation 处理完成
      setTimeout(() => {
        this.requestMissedMessages().catch((err) => {
          loggers.websocket.error('重连后请求遗漏消息失败:', err)
        })
      }, 500)
    }

    // 触发连接事件
    this.emit(INTERNAL_EVENTS.CONNECT, {})
  }

  /**
   * 处理接收到的消息
   *
   * 后端新协议：data 在顶层，不再嵌套 data.data
   * 所有事件统一透传 data，不做额外数据转换
   */
  private async handleMessage(event: MessageEvent): Promise<void> {
    try {
      let messageData: string

      // 处理不同类型的消息数据
      if (event.data instanceof Blob) {
        const arrayBuffer = await event.data.arrayBuffer()
        messageData = new TextDecoder().decode(new Uint8Array(arrayBuffer))
      } else if (typeof event.data === 'string') {
        messageData = event.data
      } else {
        messageData = String(event.data)
      }

      const rawMessage = JSON.parse(messageData)

      // 验证消息格式
      if (!rawMessage || typeof rawMessage !== 'object' || !('type' in rawMessage)) {
        loggers.websocket.warn('无效消息格式:', rawMessage)
        return
      }

      const { type, ...data } = rawMessage as Record<string, unknown>

      // 跟踪最后收到的 request_id（用于重连后请求遗漏消息）
      const requestId = rawMessage.request_id as string | undefined
      if (requestId) {
        this.lastReceivedRequestId = requestId
      }

      // 记录消息接收监控数据（心跳消息除外）
      if (type !== WS_SERVER_EVENTS.HEARTBEAT) {
        this.monitor.recordMessageReceived(type, messageData.length)
        loggers.websocket.debug(`收到消息: ${type}`, data)
      }

      // 如果消息要求 ACK 确认，自动发送 ACK
      if (rawMessage.requires_ack === true && requestId) {
        this.sendAck(requestId).catch((err) => {
          loggers.websocket.error('发送 ACK 失败:', err)
        })
      }

      // 统一的消息路由
      switch (type) {
        case WS_SERVER_EVENTS.CONNECTION_CONFIRMATION:
          // 存储协商后的协议版本
          if (data.version) {
            this.negotiatedVersion = data.version as string
          }
          this.emit(WS_SERVER_EVENTS.CONNECTION_CONFIRMATION, data)
          break

        case WS_SERVER_EVENTS.NEW_MESSAGE:
          this.emit(WS_SERVER_EVENTS.NEW_MESSAGE, data)
          break

        case WS_SERVER_EVENTS.STREAM_START:
          this.emit(WS_SERVER_EVENTS.STREAM_START, data)
          break

        case WS_SERVER_EVENTS.STREAM_CHUNK:
          this.emit(WS_SERVER_EVENTS.STREAM_CHUNK, data)
          break

        case WS_SERVER_EVENTS.STREAM_END:
          this.emit(WS_SERVER_EVENTS.STREAM_END, data)
          break

        case WS_SERVER_EVENTS.THINKING_START:
          this.emit(WS_SERVER_EVENTS.THINKING_START, data)
          break

        case WS_SERVER_EVENTS.THINKING_CHUNK:
          this.emit(WS_SERVER_EVENTS.THINKING_CHUNK, data)
          break

        case WS_SERVER_EVENTS.THINKING_END:
          this.emit(WS_SERVER_EVENTS.THINKING_END, data)
          break

        case WS_SERVER_EVENTS.TOOL_START:
          this.emit(WS_SERVER_EVENTS.TOOL_START, data)
          break

        case WS_SERVER_EVENTS.TOOL_RESULT:
          this.emit(WS_SERVER_EVENTS.TOOL_RESULT, data)
          break

        case WS_SERVER_EVENTS.EXECUTION_START:
          this.emit(WS_SERVER_EVENTS.EXECUTION_START, data)
          break

        case WS_SERVER_EVENTS.EXECUTION_PROGRESS:
          this.emit(WS_SERVER_EVENTS.EXECUTION_PROGRESS, data)
          break

        case WS_SERVER_EVENTS.EXECUTION_OUTPUT:
          this.emit(WS_SERVER_EVENTS.EXECUTION_OUTPUT, data)
          break

        case WS_SERVER_EVENTS.EXECUTION_DONE:
          this.emit(WS_SERVER_EVENTS.EXECUTION_DONE, data)
          break

        case WS_SERVER_EVENTS.EXECUTION_CANCELLED:
          this.emit(WS_SERVER_EVENTS.EXECUTION_CANCELLED, data)
          break

        case WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE:
          this.emit(WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE, data)
          break

        case WS_SERVER_EVENTS.EXECUTION_CONTROL_RESPONSE:
          this.emit(WS_SERVER_EVENTS.EXECUTION_CONTROL_RESPONSE, data)
          break

        case WS_SERVER_EVENTS.AGENT_INJECT_RESPONSE:
          this.emit(WS_SERVER_EVENTS.AGENT_INJECT_RESPONSE, data)
          break

        case WS_SERVER_EVENTS.INTERACTION_TIMEOUT_REMINDER:
          this.emit(WS_SERVER_EVENTS.INTERACTION_TIMEOUT_REMINDER, data)
          break

        case WS_SERVER_EVENTS.INTERACTION_REQUEST:
          this.emit(WS_SERVER_EVENTS.INTERACTION_REQUEST, data)
          break

        case WS_SERVER_EVENTS.MESSAGE_CHANGE:
          this.emit(WS_SERVER_EVENTS.MESSAGE_CHANGE, data)
          break

        case WS_SERVER_EVENTS.MESSAGE_DELETED:
          this.emit(WS_SERVER_EVENTS.MESSAGE_DELETED, data)
          break

        case WS_SERVER_EVENTS.MESSAGE_UPDATED:
          this.emit(WS_SERVER_EVENTS.MESSAGE_UPDATED, data)
          break

        case WS_SERVER_EVENTS.SUB_AGENT_CREATED:
          this.emit(WS_SERVER_EVENTS.SUB_AGENT_CREATED, data)
          break

        case WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT:
          this.emit(WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT, data)
          break

        case WS_SERVER_EVENTS.SUB_AGENT_COMPLETED:
          this.emit(WS_SERVER_EVENTS.SUB_AGENT_COMPLETED, data)
          break

        case WS_SERVER_EVENTS.AGENT_LEVEL_CHANGED:
          this.emit(WS_SERVER_EVENTS.AGENT_LEVEL_CHANGED, data)
          break

        case WS_SERVER_EVENTS.SCHEMA_UPDATED:
          this.emit(WS_SERVER_EVENTS.SCHEMA_UPDATED, data)
          break

        case WS_SERVER_EVENTS.ITERATION_START:
          this.emit(WS_SERVER_EVENTS.ITERATION_START, data)
          break

        case WS_SERVER_EVENTS.ITERATION_END:
          this.emit(WS_SERVER_EVENTS.ITERATION_END, data)
          break

        case WS_SERVER_EVENTS.HEARTBEAT:
          this.handleHeartbeatResponse()
          break

        case WS_SERVER_EVENTS.MISSED_MESSAGES:
          this.handleMissedMessages(data)
          break

        case 'iteration':
          this.emit(WS_SERVER_EVENTS.ITERATION_START, data)
          break

        default:
          loggers.websocket.warn('未知事件类型:', type, data)
          this.emit(type as string, data)
          break
      }
    } catch (error) {
      loggers.websocket.error('解析消息失败:', error)
    }
  }

  /**
   * 处理连接错误
   */
  private async handleError(event: Event): Promise<void> {
    loggers.websocket.error('连接错误:', event)
    this.monitor.recordError('connection_error', 'WebSocket 连接错误')

    const result = await this.errorHandler.handleError(event, {
      threadId: this.threadId,
      token: this.token ? '***' : null,
      status: this.status,
    })

    this.status = WebSocketStatus.FAILED

    this.emit(INTERNAL_EVENTS.ERROR, {
      error: event,
      userMessage: result.userMessage,
      requiresUserAction: result.requiresUserAction,
      suggestedAction: result.suggestedAction,
    })
  }

  /**
   * 处理连接关闭
   *
   * 关闭码：
   * - 4001: 认证失败，不重连
   * - 4002: 令牌过期，触发令牌刷新
   * - 4003: 连接数超限，不重连
   * - 4004: 连接被替换，不重连
   * - 其他非正常关闭: 尝试重连
   */
  private async handleClose(event: CloseEvent): Promise<void> {
    loggers.websocket.info('连接已关闭:', event.code, event.reason)
    this.clearTimers()

    const isFailed = event.code !== 1000 && event.code !== 1001
    this.monitor.recordConnectionEnd(isFailed)

    this.messageQueue.pause()

    const result = await this.errorHandler.handleError(event, {
      threadId: this.threadId,
      token: this.token ? '***' : null,
      status: this.status,
      manualDisconnect: this.manualDisconnect,
    })

    const isAuthError = event.code === 4001 || event.code === 4002
    const isConnectionLimitError = event.code === 4003

    if (!this.manualDisconnect && result.shouldRetry) {
      this.status = WebSocketStatus.RECONNECTING
      this.handleReconnectWithDelay(result.retryDelay)
    } else if (isAuthError) {
      this.status = WebSocketStatus.FAILED
      loggers.websocket.error('认证失败，需要重新登录')
      this.emit('auth_error', {
        code: event.code,
        reason: event.reason,
        userMessage: result.userMessage,
        suggestedAction: result.suggestedAction,
      })
    } else if (isConnectionLimitError) {
      this.status = WebSocketStatus.FAILED
      loggers.websocket.error('连接数超限，请刷新页面或稍后重试')
      this.emit('connection_limit_error', {
        code: event.code,
        reason: event.reason,
        userMessage: result.userMessage,
        suggestedAction: result.suggestedAction,
      })
    } else if (event.code === 4004) {
      loggers.websocket.info('连接被新连接替换，不触发重连')
      this.status = WebSocketStatus.DISCONNECTED
    } else {
      this.status = WebSocketStatus.DISCONNECTED
    }

    this.emit(INTERNAL_EVENTS.DISCONNECT, {
      code: event.code,
      reason: event.reason,
      manual: this.manualDisconnect,
      userMessage: result.userMessage,
      requiresUserAction: result.requiresUserAction,
      suggestedAction: result.suggestedAction,
    })
  }

  /**
   * 使用指定延迟进行重连
   *
   * @param delay 重连延迟（毫秒）
   */
  private handleReconnectWithDelay(delay: number): void {
    loggers.reconnect.info(`将在 ${delay}ms 后进行第 ${this.reconnectAttempts + 1} 次重连`)

    this.reconnectAttempts++

    this.reconnectTimer = setTimeout(async () => {
      if (this.manualDisconnect) {
        return
      }

      try {
        const currentToken = tokenManager.getToken()

        if (currentToken) {
          try {
            await apiClient.get('/api/v1/auth/me')
            loggers.reconnect.debug('Token 验证成功，准备重连')
          } catch (error) {
            const newToken = tokenManager.getToken()
            if (newToken && newToken !== currentToken) {
              loggers.reconnect.debug('Token 已通过拦截器自动刷新')
              this.token = newToken
            } else {
              throw error
            }
          }

          const latestToken = tokenManager.getToken()
          if (latestToken && this.threadId) {
            this.token = latestToken
            this.connect(this.threadId, this.token)
          } else {
            throw new Error('无法获取有效的认证令牌')
          }
        } else {
          throw new Error('未找到认证令牌')
        }
      } catch (error) {
        loggers.reconnect.error('Token 验证/刷新失败，无法重连:', error)
        this.status = WebSocketStatus.FAILED
        this.emit('auth_error', {
          code: 4002,
          reason: 'Token 刷新失败',
          userMessage: '认证失败，请重新登录',
          suggestedAction: '请点击重新登录',
        })
      }
    }, delay)
  }

  /**
   * 处理重连逻辑（指数退避）
   */
  private handleReconnect(): void {
    if (this.reconnectAttempts >= WS_RECONNECT_CONFIG.MAX_RETRIES) {
      loggers.reconnect.error('达到最大重连次数，停止重连')
      this.status = WebSocketStatus.FAILED
      return
    }

    this.monitor.recordReconnect()

    const delay = Math.min(
      WS_RECONNECT_CONFIG.INITIAL_DELAY *
        Math.pow(WS_RECONNECT_CONFIG.BACKOFF_FACTOR, this.reconnectAttempts),
      WS_RECONNECT_CONFIG.MAX_DELAY,
    )

    this.handleReconnectWithDelay(delay)
  }

  // ============================================
  // 心跳管理
  // ============================================

  /**
   * 启动心跳
   */
  private startHeartbeat(): void {
    this.clearHeartbeatTimers()

    this.heartbeatTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.sendHeartbeat()

        this.heartbeatTimeoutTimer = setTimeout(() => {
          loggers.heartbeat.warn('心跳超时，关闭连接')
          this.ws?.close()
        }, WS_HEARTBEAT_CONFIG.TIMEOUT)
      }
    }, WS_HEARTBEAT_CONFIG.INTERVAL)
  }

  /**
   * 处理心跳响应
   */
  private handleHeartbeatResponse(): void {
    if (this.heartbeatTimeoutTimer) {
      clearTimeout(this.heartbeatTimeoutTimer)
      this.heartbeatTimeoutTimer = null
    }
  }

  // ============================================
  // 定时器管理
  // ============================================

  /**
   * 清除所有定时器
   */
  private clearTimers(): void {
    this.clearReconnectTimer()
    this.clearHeartbeatTimers()
  }

  /**
   * 清除所有 ACK 待确认定时器
   */
  private clearAckTimers(): void {
    for (const [, pending] of this.pendingAckTimers) {
      clearTimeout(pending.timer)
    }
    this.pendingAckTimers.clear()
  }

  /**
   * 清除重连定时器
   */
  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  /**
   * 清除心跳定时器
   */
  private clearHeartbeatTimers(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }

    if (this.heartbeatTimeoutTimer) {
      clearTimeout(this.heartbeatTimeoutTimer)
      this.heartbeatTimeoutTimer = null
    }
  }

  // ============================================
  // 事件分发
  // ============================================

  /**
   * 触发事件
   *
   * @param event 事件类型
   * @param data 事件数据
   */
  private emit(event: string, data: unknown): void {
    const handlers = this.eventHandlers.get(event)
    if (handlers) {
      handlers.forEach((handler) => {
        try {
          handler(data)
        } catch (error) {
          loggers.eventHandler.error(`事件处理器执行失败 (${event}):`, error)
        }
      })
    }
  }

  // ============================================
  // 消息队列管理方法
  // ============================================

  /**
   * 获取消息队列状态
   */
  async getQueueStatus() {
    const stats = await this.messageQueue.getStats()
    const status = this.messageQueue.getStatus()
    return {
      size: stats.total,
      paused: status.paused,
      pending: stats.pending,
      retrying: stats.sending + stats.failed,
      sending: stats.sending,
      sent: stats.sent,
      failed: stats.failed,
      total: stats.total,
    }
  }

  /**
   * 暂停消息队列处理
   */
  pauseQueue(): void {
    this.messageQueue.pause()
    loggers.messageQueue.info('消息队列已暂停')
  }

  /**
   * 恢复消息队列处理
   */
  resumeQueue(): void {
    this.messageQueue.resume()
    loggers.messageQueue.info('消息队列已恢复')
  }

  /**
   * 清空消息队列
   */
  async clearQueue(): Promise<void> {
    await this.messageQueue.clear()
    loggers.messageQueue.info('消息队列已清空')
  }

  // ============================================
  // ACK 确认机制
  // ============================================

  /**
   * 发送 ACK 确认消息
   *
   * 当收到 requires_ack=true 的服务端消息时，
   * 自动发送 ACK 以确认前端已收到该消息。
   *
   * @param requestId 被确认的消息 request_id
   */
  async sendAck(requestId: string): Promise<boolean> {
    const ackMessage: MessageAckMessage = {
      type: WS_CLIENT_MESSAGES.MESSAGE_ACK,
      request_id: requestId,
      received_at: new Date().toISOString(),
    }

    try {
      await this.sendMessageDirect(
        ackMessage as unknown as WebSocketClientMessage,
      )
      // 清除该消息的 ACK 重试定时器（如果存在）
      const pending = this.pendingAckTimers.get(requestId)
      if (pending) {
        clearTimeout(pending.timer)
        this.pendingAckTimers.delete(requestId)
      }
      return true
    } catch (error) {
      loggers.websocket.error('ACK 发送失败:', error)
      return false
    }
  }

  // ============================================
  // 重连优化：遗漏消息恢复
  // ============================================

  /**
   * 请求遗漏消息
   *
   * 重连后向前端发送 request_missed 消息，
   * 携带最后收到的 request_id，请求服务端
   * 补发断线期间遗漏的消息。
   */
  async requestMissedMessages(): Promise<boolean> {
    const requestMessage: RequestMissedMessage = {
      type: WS_CLIENT_MESSAGES.REQUEST_MISSED,
      last_received_request_id: this.lastReceivedRequestId,
    }

    try {
      await this.sendMessageDirect(
        requestMessage as unknown as WebSocketClientMessage,
      )
      loggers.websocket.info(
        '已请求遗漏消息, last_request_id:',
        this.lastReceivedRequestId,
      )
      return true
    } catch (error) {
      loggers.websocket.error('请求遗漏消息失败:', error)
      return false
    }
  }

  /**
   * 处理遗漏消息响应
   *
   * 服务端返回 missed_messages 事件后，
   * 按顺序重放遗漏的消息。
   *
   * @param data missed_messages 事件数据
   */
  private handleMissedMessages(data: unknown): void {
    const missedData = data as {
      messages: Array<Record<string, unknown>>
      total: number
      has_more: boolean
    }

    loggers.websocket.info(
      `收到遗漏消息: ${missedData.total} 条`,
    )

    // 重放遗漏的消息
    for (const msg of missedData.messages) {
      const msgType = msg.type as string
      if (msgType && msg.type !== WS_SERVER_EVENTS.HEARTBEAT) {
        this.emit(msgType, msg)
      }
    }

    // 更新最后收到的 request_id
    if (missedData.messages.length > 0) {
      const lastMsg = missedData.messages[missedData.messages.length - 1]
      const lastRid = lastMsg.request_id as string | undefined
      if (lastRid) {
        this.lastReceivedRequestId = lastRid
      }
    }

    // 如果还有更多消息，继续请求
    if (missedData.has_more) {
      this.requestMissedMessages().catch((err) => {
        loggers.websocket.error('继续请求遗漏消息失败:', err)
      })
    }
  }

  /**
   * 获取协商后的协议版本
   */
  getNegotiatedVersion(): string {
    return this.negotiatedVersion
  }

  // ============================================
  // 性能监控方法
  // ============================================

  /**
   * 获取性能统计信息
   */
  async getPerformanceStats() {
    const queueStats = await this.messageQueue.getStats()
    return {
      monitor: this.monitor.getMetrics(),
      heartbeat: this.heartbeatManager.getNetworkStats(),
      messageQueue: {
        ...this.messageQueue.getStatus(),
        ...queueStats,
      },
    }
  }

  /**
   * 获取网络质量
   */
  getNetworkQuality() {
    return this.heartbeatManager.getNetworkQuality()
  }

  /**
   * 重置性能统计
   */
  resetPerformanceStats() {
    this.monitor.reset()
    this.heartbeatManager.reset()
  }

  /**
   * 导出性能数据
   */
  exportPerformanceData() {
    return {
      monitor: this.monitor.exportData(),
      heartbeat: this.heartbeatManager.getNetworkStats(),
      timestamp: new Date().toISOString(),
    }
  }
}

/** WebSocket 服务单例实例 */
export const webSocketService = new WebSocketService()
export default webSocketService
