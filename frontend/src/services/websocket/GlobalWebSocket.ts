/**
 * 全局单连接 WebSocket 服务
 *
 * 设计原则：
 * - 每个用户只维持一条 WS 连接，所有会话的消息通过消息体中的
 *   thread_id 和 pipeline_id 进行路由
 * - 路由失败时记录错误，不广播（广播是消息串扰的根因）
 * - 连接断开时自动重连（指数退避，最大 10 秒间隔）
 */

import { buildGlobalWebSocketUrl } from '@/constants/websocket'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected'

interface PendingMessage {
  type: string
  [key: string]: unknown
}

type EventHandler = (data: any) => void

const RECONNECT_BASE_DELAY = 1000
const RECONNECT_MAX_DELAY = 30_000
const RECONNECT_MAX_RETRIES = 30
const HEARTBEAT_INTERVAL = 30_000
const HEARTBEAT_TIMEOUT = 30_000
const CONNECTION_TIMEOUT = 15_000

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
    console.log('[GlobalWS] connecting to', url.substring(0, 60))
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
      console.log('[GlobalWS] connected')
      this._status = 'connected'
      this._reconnectAttempts = 0
      this._flushQueue()
      this._startHeartbeat()
      this._emit('_status', { status: 'connected' })
      this._emit('connect', { status: 'connected' })
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

  /** 向指定会话发送用户输入 */
  sendUserInput(threadId: string, content: string, opts?: {
    pipelineId?: string
    attachments?: unknown[]
    enableThinking?: boolean
    clientMessageId?: string
  }): void {
    this._send({
      type: 'user_input',
      thread_id: threadId,
      content,
      pipeline_id: opts?.pipelineId || '',
      attachments: opts?.attachments || [],
      enable_thinking: opts?.enableThinking || false,
      client_message_id: opts?.clientMessageId || '',
    })
  }

  /** 发送审批决策 */
  sendApproval(threadId: string, decision: string, reason?: string): void {
    this._send({ type: 'approval', thread_id: threadId, decision, reason })
  }

  /** 取消生成 */
  sendCancel(threadId: string, reason?: string): void {
    this._send({ type: 'stop_generation', thread_id: threadId, reason })
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

  private _send(msg: PendingMessage): void {
    if (this._status === 'connected' && this.ws) {
      try {
        this.ws.send(JSON.stringify(msg))
      } catch {
        this._queue.push(msg)
      }
    } else {
      this._queue.push(msg)
    }
  }

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
