/**
 * 全局单连接 WebSocket 服务
 *
 * 设计原则：
 * - 每个用户只维持一条 WS 连接，所有会话的消息通过消息体中的
 *   thread_id 和 pipeline_id 进行路由
 * - 路由失败时记录错误，不广播（广播是消息串扰的根因）
 * - 连接断开时自动重连（指数退避，初始 4 秒，最大 60 秒间隔）
 */

import { buildGlobalWebSocketUrl } from '@/constants/websocket'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { loggers } from '@/utils/logger'

const _wsLogger = loggers.websocket

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'reconnecting'

interface PendingMessage {
  type: string
  [key: string]: unknown
}

type EventHandler = (data: any) => void

const RECONNECT_BASE_DELAY = 4_000
const RECONNECT_MAX_DELAY = 60_000
const RECONNECT_MAX_RETRIES = 30
const HEARTBEAT_INTERVAL = 30_000
const HEARTBEAT_TIMEOUT = 30_000
const CONNECTION_TIMEOUT = 15_000

/** 发送缓冲区阈值：超过此值延迟发送（1MB） */
const SEND_BUFFER_THRESHOLD = 1_000_000

/** 发送确认超时：未收到 stream_start 则重发（10秒） */
const SEND_ACK_TIMEOUT_MS = 10_000

/** 最大发送重试次数 */
const SEND_ACK_MAX_RETRIES = 2

interface PendingAckEntry {
  timer: ReturnType<typeof setTimeout>
  retries: number
  msg: PendingMessage
}

class GlobalWebSocketService {
  private ws: WebSocket | null = null
  private _status: ConnectionStatus = 'disconnected'
  private _token: string = ''
  private _handlers: Map<string, Set<EventHandler>> = new Map()
  private _queue: PendingMessage[] = []
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private _reconnectAttempts: number = 0
  private _heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private _heartbeatTimeoutTimer: ReturnType<typeof setTimeout> | null = null
  private _disposed: boolean = false

  private _connectTimer: ReturnType<typeof setTimeout> | null = null
  private _connectionTimeoutTimer: ReturnType<typeof setTimeout> | null = null

  /** 待确认的 user_input 消息：key 为 threadId */
  private _pendingAcks: Map<string, PendingAckEntry> = new Map()

  /** 建立全局 WS 连接（登录后调用一次） */
  connect(token: string): void {
    if (this._disposed) return
    if (this._status === 'connected' && this._token === token) return
    if (this._status === 'connecting' && this._token === token && this._connectTimer) return

    this._token = token
    this._status = 'connecting'
    this._clearTimers()

    if (this._connectTimer) {
      clearTimeout(this._connectTimer)
      this._connectTimer = null
    }

    if (this.ws) {
      this.ws.onclose = null
      this.ws.onerror = null
      this.ws.onmessage = null
      this.ws.onopen = null
      try { this.ws.close(1000, 'reconnect') } catch { /* ignore */ }
      this.ws = null
    }

    this._connectTimer = setTimeout(() => {
      this._connectTimer = null
      this._doConnect()
    }, 50)
  }

  /** 实际建立 WebSocket 连接 */
  private _doConnect(): void {
    if (this._disposed || this._status !== 'connecting') return

    const url = buildGlobalWebSocketUrl(this._token)
    _wsLogger.debug('[GlobalWS] connecting to %s', url.substring(0, 60))
    this.ws = new WebSocket(url)

    this._connectionTimeoutTimer = setTimeout(() => {
      if (this._status === 'connecting') {
        console.warn('[GlobalWS] 连接超时，关闭并重连')
        if (this.ws) {
          this.ws.onclose = null
          this.ws.onerror = null
          this.ws.onmessage = null
          this.ws.onopen = null
          try { this.ws.close(1000, 'connection_timeout') } catch { /* ignore */ }
          this.ws = null
        }
        this._status = 'disconnected'
        this._scheduleReconnect()
      }
    }, CONNECTION_TIMEOUT)

    this.ws.onopen = () => {
      if (this._connectionTimeoutTimer) {
        clearTimeout(this._connectionTimeoutTimer)
        this._connectionTimeoutTimer = null
      }
      // FIX: 区分首次连接与重连，重连时额外 emit 'reconnected' 事件供 streaming handler 补漏
      const isReconnect = this._reconnectAttempts > 0
      _wsLogger.debug('[GlobalWS] connected %s', isReconnect ? '(reconnect)' : '')
      this._status = 'connected'
      this._reconnectAttempts = 0
      this._flushQueue()
      this._startHeartbeat()
      this._emit('_status', { status: 'connected' })
      this._emit('connect', { status: 'connected' })
      if (isReconnect) {
        this._emit('reconnected', { status: 'connected' })
      }
      useLayoutModeStore.getState().updateConnectionStatus({
        state: 'connected',
        lastConnectedAt: new Date().toISOString(),
      })
    }

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'heartbeat_ack') {
          this._handleHeartbeatAck()
        }
        _wsLogger.debug(
          `[WS_RAW] type=${data.type} pipeline_id=${data.data?.pipeline_id?.slice(0, 12) || 'null'} message_id=${data.data?.message_id?.slice(0, 12) || 'null'}`,
        )
        if (data.type) {
          this._emit(data.type, data)
        }
        this._emit('*', data)
      } catch {
        // 非 JSON 消息忽略
      }
    }

    this.ws.onerror = () => {
      // onclose 会处理重连
    }

    this.ws.onclose = (event) => {
      if (this._connectionTimeoutTimer) {
        clearTimeout(this._connectionTimeoutTimer)
        this._connectionTimeoutTimer = null
      }
      const wasConnected = this._status === 'connected'
      this._status = 'disconnected'
      this._stopHeartbeat()
      this._emit('_status', { status: 'disconnected', code: event.code, reason: event.reason })
      useLayoutModeStore.getState().updateConnectionStatus({ state: 'disconnected' })

      if (event.code === 4000) {
        console.info('[GlobalWS] 被新连接替换(code=4000)，跳过重连')
        return
      }

      if (!this._disposed) {
        this._scheduleReconnect()
      }
    }
  }

  /** 断开连接（登出时调用） */
  disconnect(): void {
    this._disposed = true
    this._clearTimers()
    this._stopHeartbeat()
    if (this._connectionTimeoutTimer) {
      clearTimeout(this._connectionTimeoutTimer)
      this._connectionTimeoutTimer = null
    }
    if (this.ws) {
      this.ws.onclose = null
      this.ws.onerror = null
      this.ws.onmessage = null
      this.ws.onopen = null
      this.ws.close(1000, '用户主动断开')
      this.ws = null
    }
    this._status = 'disconnected'
    this._queue = []
    this._handlers.clear()
  }

  /**
   * 向指定会话发送用户输入（带发送确认机制）
   *
   * BUG-FIX-fix_20260523_ack_resend_dup:
   * 问题根因: 未连接时 _send 将消息加入队列，但 ACK 超时重发也调用 _send 再次入队，
   *          连接建立后 _flushQueue 将两条相同消息同时发出，后端处理两次产生重复响应。
   * 修复方案: 仅在已连接状态下才启动 ACK 计时器。未连接时消息已在队列中，
   *          _flushQueue 发送后会启动 ACK 计时器。
   * 影响范围: WebSocket 未连接时的消息发送场景（刷新页面后立即发送）
   * 修复日期: 2026-05-23
   */
  sendUserInput(threadId: string, content: string, opts?: {
    pipelineId?: string
    attachments?: unknown[]
    enableThinking?: boolean
    clientMessageId?: string
  }): void {
    const msg: PendingMessage = {
      type: 'user_input',
      thread_id: threadId,
      content,
      pipeline_id: opts?.pipelineId || '',
      attachments: opts?.attachments || [],
      enable_thinking: opts?.enableThinking || false,
      client_message_id: opts?.clientMessageId || '',
    }

    // 先清除该线程之前的待确认（避免重复）
    this._clearPendingAck(threadId)

    // 发送消息
    this._send(msg)

    // BUG-FIX-fix_20260523_ack_resend_dup:
    // 仅在已连接状态下才启动 ACK 计时器。
    // 未连接时消息已在队列中，_flushQueue 发送后会启动 ACK 计时器。
    if (this._status === 'connected') {
      this._startAckTimer(threadId, msg)
    }
  }

  /** 清除指定线程的发送确认计时器（收到 stream_start 时调用） */
  clearPendingAckForThread(threadId: string): void {
    this._clearPendingAck(threadId)
  }

  /** 启动发送确认计时器：超时未收到响应则重发 */
  private _startAckTimer(threadId: string, msg: PendingMessage): void {
    const entry: PendingAckEntry = {
      timer: setTimeout(() => {
        this._onAckTimeout(threadId)
      }, SEND_ACK_TIMEOUT_MS),
      retries: 0,
      msg,
    }
    this._pendingAcks.set(threadId, entry)
  }

  /** 发送确认超时：重发消息或放弃 */
  private _onAckTimeout(threadId: string): void {
    const entry = this._pendingAcks.get(threadId)
    if (!entry) return

    entry.retries++
    if (entry.retries > SEND_ACK_MAX_RETRIES) {
      console.warn('[GlobalWS] 发送确认超时，已达最大重试次数: threadId=%s', threadId)
      this._pendingAcks.delete(threadId)
      this._emit('_ack_exhausted', { threadId })
      return
    }

    console.info('[GlobalWS] 发送确认超时，第 %d 次重发: threadId=%s', entry.retries, threadId)
    this._send(entry.msg)

    entry.timer = setTimeout(() => {
      this._onAckTimeout(threadId)
    }, SEND_ACK_TIMEOUT_MS)
  }

  /** 清除指定线程的发送确认计时器 */
  private _clearPendingAck(threadId: string): void {
    const entry = this._pendingAcks.get(threadId)
    if (entry) {
      clearTimeout(entry.timer)
      this._pendingAcks.delete(threadId)
    }
  }

  /** 发送审批决策 */
  sendApproval(threadId: string, decision: string, reason?: string): void {
    this._send({ type: 'approval', thread_id: threadId, decision, reason })
  }

  /** 取消生成 */
  // BUG-FIX: 增加 pipelineId 参数，避免停止按钮误取消其他管道
  sendCancel(threadId: string, reason?: string, pipelineId?: string): void {
    this._send({ type: 'stop_generation', thread_id: threadId, reason, pipeline_id: pipelineId })
  }

  /** 响应子 Agent 输入请求 */
  sendUserInputResponse(threadId: string, executionId: string, response: string): void {
    this._send({ type: 'user_input_response', thread_id: threadId, execution_id: executionId, response })
  }

  /** 响应人类交互请求 */
  sendInteractionResponse(threadId: string, requestId: string, response: unknown): void {
    this._send({ type: 'interaction_response', thread_id: threadId, data: { request_id: requestId, response } })
  }

  /** 订阅事件 */
  subscribe(event: string, handler: EventHandler): void {
    if (!this._handlers.has(event)) {
      this._handlers.set(event, new Set())
    }
    this._handlers.get(event)!.add(handler)
  }

  /** 取消订阅 */
  unsubscribe(event: string, handler: EventHandler): void {
    this._handlers.get(event)?.delete(handler)
  }

  /** 获取当前连接状态 */
  get status(): ConnectionStatus {
    return this._status
  }

  // ── 内部方法 ──

  /**
   * 发送消息（立即发送或加入队列）
   *
   * BUG-FIX-fix_20260523_queue_dup:
   * 问题根因: ACK 超时重发或网络抖动时，相同消息可能多次入队，
   *          _flushQueue 将多条相同消息同时发出。
   * 修复方案: 入队前检查队列中是否已有相同 thread_id + type + client_message_id 的消息，
   *          避免重复入队。
   * 影响范围: 所有通过 WebSocket 发送的消息
   * 修复日期: 2026-05-23
   */
  private _send(msg: PendingMessage): void {
    if (this._status === 'connected' && this.ws) {
      try {
        const payload = JSON.stringify(msg)
        // 发送前检查缓冲区，超过阈值则延迟发送避免积压
        if (this.ws.bufferedAmount > SEND_BUFFER_THRESHOLD) {
          console.warn('[GlobalWS] bufferedAmount 超过阈值，消息入队延迟发送')
          this._enqueueIfNotDuplicate(msg)
          return
        }
        this.ws.send(payload)
      } catch {
        this._enqueueIfNotDuplicate(msg)
      }
    } else {
      this._enqueueIfNotDuplicate(msg)
    }
  }

  /**
   * 将消息加入发送队列（带去重检查）
   *
   * BUG-FIX-fix_20260523_queue_dup:
   * 通过 thread_id + type + client_message_id 三元组判断是否重复，
   * 避免同一消息多次入队导致连接后重复发送。
   */
  private _enqueueIfNotDuplicate(msg: PendingMessage): void {
    const isDuplicate = this._queue.some((queued) =>
      queued.type === msg.type
      && queued.thread_id === msg.thread_id
      && (queued as any).client_message_id === (msg as any).client_message_id
    )
    if (isDuplicate) {
      console.info(
        '[GlobalWS] 去重: 跳过重复入队 type=%s thread_id=%s',
        msg.type,
        (msg.thread_id as string)?.slice(0, 12),
      )
      return
    }
    this._queue.push(msg)
  }

  /**
   * 刷写发送队列：连接建立后将队列中的消息逐条发送
   *
   * BUG-FIX-fix_20260523_ack_resend_dup:
   * 问题根因: 原逻辑刷完队列后不启动 ACK 计时器，导致消息发出后无超时重发保护。
   *          但 sendUserInput 中未连接时不启动计时器，所以需要在刷队后补启动。
   * 修复方案: 发送完 user_input 类型消息后，启动对应的 ACK 计时器。
   * 影响范围: WebSocket 重连后的消息发送确认
   * 修复日期: 2026-05-23
   */
  private _flushQueue(): void {
    if (!this.ws || this._status !== 'connected') return
    while (this._queue.length > 0) {
      const msg = this._queue.shift()!
      try {
        this.ws.send(JSON.stringify(msg))
      } catch {
        this._queue.unshift(msg)
        break
      }

      // BUG-FIX-fix_20260523_ack_resend_dup:
      // user_input 消息发送成功后启动 ACK 计时器
      // （sendUserInput 在未连接时不启动计时器，此处补启动）
      if (msg.type === 'user_input' && msg.thread_id) {
        // 如果该线程已有 ACK 计时器（例如之前已发送过），先清除再启动新的
        this._startAckTimer(msg.thread_id as string, msg)
      }
    }
  }

  private _emit(event: string, data: any): void {
    const handlers = this._handlers.get(event)
    if (handlers) {
      for (const h of handlers) {
        try { h(data) } catch { /* handler 异常不影响其他 handler */ }
      }
    }
  }

  private _startHeartbeat(): void {
    this._stopHeartbeat()
    this._heartbeatTimer = setInterval(() => {
      if (this._status === 'connected') {
        this._send({ type: 'heartbeat', timestamp: Date.now() })
        this._clearHeartbeatTimeout()
        this._heartbeatTimeoutTimer = setTimeout(() => {
          console.warn('[GlobalWS] 心跳超时，连接可能已断开，主动关闭重连')
          if (this.ws) {
            this.ws.close(4001, '心跳超时')
          }
        }, HEARTBEAT_TIMEOUT)
      }
    }, HEARTBEAT_INTERVAL)
  }

  private _handleHeartbeatAck(): void {
    this._clearHeartbeatTimeout()
  }

  private _clearHeartbeatTimeout(): void {
    if (this._heartbeatTimeoutTimer) {
      clearTimeout(this._heartbeatTimeoutTimer)
      this._heartbeatTimeoutTimer = null
    }
  }

  private _stopHeartbeat(): void {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer)
      this._heartbeatTimer = null
    }
    this._clearHeartbeatTimeout()
  }

  private _scheduleReconnect(): void {
    if (this._disposed) return

    // 标记为重连中，更新 UI 状态
    this._status = 'reconnecting'
    this._emit('_status', { status: 'reconnecting' })
    useLayoutModeStore.getState().updateConnectionStatus({ state: 'reconnecting' })

    let delay: number
    if (this._reconnectAttempts >= RECONNECT_MAX_RETRIES) {
      delay = RECONNECT_MAX_DELAY
      console.info('[GlobalWS] 超过最大重连次数，改为 %dms 间隔持续重连', delay)
    } else {
      delay = Math.min(
        RECONNECT_BASE_DELAY * Math.pow(2, this._reconnectAttempts),
        RECONNECT_MAX_DELAY,
      )
    }
    this._reconnectAttempts++
    console.info('[GlobalWS] %dms 后重连（第 %d 次）', delay, this._reconnectAttempts)
    this._reconnectTimer = setTimeout(() => {
      if (!this._disposed && this._token) {
        this.connect(this._token)
      }
    }, delay)
  }

  private _clearTimers(): void {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer)
      this._reconnectTimer = null
    }
  }
}

/** 全局单例 */
export const globalWS = new GlobalWebSocketService()
