/** 全局单连接 WebSocket 服务 设计原则： */

import { buildGlobalWebSocketUrl } from '@/constants/websocket'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useAuthStore, isAuthFailureFromError } from '@/stores/authStore'
import { triggerAuthExpired } from '@/services/authCallbacks'
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
// 超时设 90s 并要求连续 2 次未收到 ack 才判定连接死亡：
// 局域网/非本机访问（如跨设备 ip=192.168.x.x）或后端繁忙时，单次 ack 延迟常见，
// 零容错（45s 一次超时就断）会导致 WS 每 30-45s 反复断连，流式 chunk 大量丢失。
// 连续 2 次超时（≈90s）仍能及时检测真死连接。
const HEARTBEAT_TIMEOUT = 90_000
const HEARTBEAT_MAX_MISS = 2
const CONNECTION_TIMEOUT = 15_000

/** 发送缓冲区阈值：超过此值延迟发送（1MB） */
const SEND_BUFFER_THRESHOLD = 1_000_000

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
  private _heartbeatMissCount: number = 0
  private _disposed: boolean = false

  private _connectionTimeoutTimer: ReturnType<typeof setTimeout> | null = null

  /** 建立全局 WS 连接（登录后调用一次） */
  connect(token: string): void {
    if (this._disposed) return
    if (this._status === 'connected' && this._token === token) return
    if (this._status === 'connecting' && this._token === token) return

    this._token = token
    this._status = 'connecting'
    this._clearTimers()

    if (this.ws) {
      this.ws.onclose = null
      this.ws.onerror = null
      this.ws.onmessage = null
      this.ws.onopen = null
      try { this.ws.close(1000, 'reconnect') } catch { /* ignore */ }
      this.ws = null
    }

    this._doConnect()
  }

  /** 实际建立 WebSocket 连接 */
  private _doConnect(): void {
    if (this._disposed || this._status !== 'connecting') return

    const url = buildGlobalWebSocketUrl(this._token)
    _wsLogger.debug('[GlobalWS] connecting to %s', url.substring(0, 60))
    this.ws = new WebSocket(url)

    this._connectionTimeoutTimer = setTimeout(() => {
      if (this._status === 'connecting') {
        // -M03: WS handler 层 console 残留
        _wsLogger.warn('[GlobalWS] 连接超时，关闭并重连')
        if (this.ws) {
          this.ws.onclose = null
          this.ws.onerror = null
          this.ws.onmessage = null
          this.ws.onopen = null
          try { this.ws.close(1000, 'connection_timeout') } catch { /* ignore */ }
          this.ws = null
        }
        this._status = 'disconnected'
        // 连接超时属于网络层问题，非认证拒绝，走普通重连（不刷新 token）
        this._scheduleReconnect(false)
      }
    }, CONNECTION_TIMEOUT)

    this.ws.onopen = () => {
      if (this._connectionTimeoutTimer) {
        clearTimeout(this._connectionTimeoutTimer)
        this._connectionTimeoutTimer = null
      }
      // 区分首次连接与重连，重连时额外 emit 'reconnected' 事件供 streaming handler 补漏
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
      this._status = 'disconnected'
      this._stopHeartbeat()
      this._emit('_status', { status: 'disconnected', code: event.code, reason: event.reason })
      useLayoutModeStore.getState().updateConnectionStatus({ state: 'disconnected' })

      if (event.code === 4000) {
        console.info('[GlobalWS] 被新连接替换(code=4000)，跳过重连')
        return
      }

      if (!this._disposed) {
        // 后端 token 无效/过期时以 code=4001 关闭连接，前端需先刷新 token 再重连。
        // 任何掉线（含 1006、心跳超时 2002、4001）都先检查 token 是否已过期：
        // 已建立连接掉了（wasConnected=true）也可能是 token 过期后才掉，旧逻辑的
        // !wasConnected 门控会让此类掉线用过期 token 硬连 → 4001 → 崩溃。checkTokenExpiration
        // 只在真过期时返回 true，未过期时不触发刷新，安全。
        let authRejected = event.code === 4001
        if (!authRejected) {
          authRejected = useAuthStore.getState().checkTokenExpiration()
        }
        this._scheduleReconnect(authRejected)
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

    this._send(msg)
  }

  /** 发送审批决策 */
  sendApproval(threadId: string, decision: string, reason?: string): void {
    this._send({ type: 'approval', thread_id: threadId, decision, reason })
  }

  /** 取消生成 */
  // 增加 pipelineId 参数，避免停止按钮误取消其他管道
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

  /** 发送消息（立即发送或加入队列） */
  private _send(msg: PendingMessage): void {
    if (this._status === 'connected' && this.ws) {
      try {
        const payload = JSON.stringify(msg)
        // 发送前检查缓冲区，超过阈值则延迟发送避免积压
        if (this.ws.bufferedAmount > SEND_BUFFER_THRESHOLD) {
          _wsLogger.warn('[GlobalWS] bufferedAmount 超过阈值，消息入队延迟发送')
          this._enqueueIfNotDuplicate(msg)
          return
        }
        this.ws.send(payload)
        _wsLogger.debug('[GlobalWS] 已发送: type=%s thread=%s', msg.type, (msg as any).thread_id?.slice(0, 12))
      } catch (err) {
        _wsLogger.warn('[GlobalWS] ws.send 失败，消息入队: type=%s readyState=%s error=%s',
          msg.type, this.ws?.readyState, err instanceof Error ? err.message : String(err))
        this._enqueueIfNotDuplicate(msg)
      }
    } else {
      this._enqueueIfNotDuplicate(msg)
    }
  }

  /** 将消息加入发送队列（带去重检查） */
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
    this._heartbeatMissCount = 0
    this._heartbeatTimer = setInterval(() => {
      if (this._status === 'connected') {
        this._send({ type: 'heartbeat', timestamp: Date.now() })
        this._clearHeartbeatTimeout()
        this._heartbeatTimeoutTimer = setTimeout(() => {
          // 连续失败容错：单次 ack 超时不立即断连，累计达到 HEARTBEAT_MAX_MISS 才判定死亡。
          // 避免局域网抖动/后端繁忙时的误断（曾导致每 30-45s 反复断连、流式 chunk 丢失）。
          this._heartbeatMissCount += 1
          if (this._heartbeatMissCount >= HEARTBEAT_MAX_MISS) {
            _wsLogger.warn(
              '[GlobalWS] 心跳连续 %d 次未收到 ack，判定连接死亡，主动关闭重连',
              this._heartbeatMissCount,
            )
            if (this.ws) {
              // // 心跳超时用 code=2002（TIMEOUT），**绝不复用 4001**。
              // 4001 已被后端用于「token 无效/过期」的认证拒绝（见 app_factory.py:244/248），
              // onclose 据此触发 token 刷新路径。若心跳超时也用 4001，会被误判为认证拒绝，
              // 在无 refresh token 的环境（测试/未登录）反复抛错。心跳超时属于网络层故障，
              // 应走普通重连（直接用当前 token 重连），不触发刷新。
              this.ws.close(2002, '心跳超时')
            }
          } else {
            _wsLogger.warn(
              '[GlobalWS] 心跳 ack 超时（第 %d/%d 次），暂不断连等待下次心跳',
              this._heartbeatMissCount, HEARTBEAT_MAX_MISS,
            )
          }
        }, HEARTBEAT_TIMEOUT)
      }
    }, HEARTBEAT_INTERVAL)
  }

  private _handleHeartbeatAck(): void {
    this._clearHeartbeatTimeout()
    this._heartbeatMissCount = 0
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

  /** 调度重连 - true：需先刷新 token 再连；刷新真失效则登出并停止重连。 */
  private _scheduleReconnect(authRejected: boolean = false): void {
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
    console.info('[GlobalWS] %dms 后重连（第 %d 次, authRejected=%s）', delay, this._reconnectAttempts, authRejected)
    this._reconnectTimer = setTimeout(async () => {
      if (this._disposed || !this._token) return

      // 普通断连（非认证拒绝）：直接重连，不触碰 token
      if (!authRejected) {
        this.connect(this._token)
        return
      }

      // 认证拒绝：必须先刷新 token 再连
      const authStore = useAuthStore.getState()
      _wsLogger.info('[GlobalWS] 连接被认证拒绝(4001)，刷新 token 后再重连')
      try {
        await authStore.refreshToken()
        // 刷新成功：用新 token 重连（refreshToken 已更新 store 与 localStorage）
        const newToken = authStore.token
        if (newToken && newToken !== this._token) {
          this._token = newToken
          _wsLogger.info('[GlobalWS] Token 已刷新，用新 token 重连')
        }
        this.connect(this._token)
      } catch (refreshError) {
        if (isAuthFailureFromError(refreshError)) {
          // refresh_token 真正失效：没有可用 token，连了也是 4001。
          // 走登出流程，停止重连，让用户重新登录。
          _wsLogger.warn('[GlobalWS] refresh_token 真正失效，触发登出并停止重连')
          triggerAuthExpired()
        } else {
          // 瞬时故障（网络/超时/5xx）：不登出，按退避等下一轮再试刷新。
          // 关键：不用过期 token 连接，避免 4001 死循环。
          _wsLogger.warn('[GlobalWS] Token 刷新瞬时失败，等待下一轮重连（不登出）')
          this._scheduleReconnect(true)
        }
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
