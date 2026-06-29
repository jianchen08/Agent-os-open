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
// BUG-FIX-fix_20260628_heartbeat_zero_margin:
// 历史值 30_000 == HEARTBEAT_INTERVAL，零容错：后端 ack 稍慢（事件循环繁忙、
// 大 payload 序列化、网络抖动）就会误判连接死了 → 主动 close(2002) 重连。
// LLM 流式期间后端事件循环负载高（chunk_consumer + json.dumps 推送），
// heartbeat_ack 响应极易突破 30s → 频繁误断。提到 45s，明确大于 INTERVAL，
// 给 ack 留容错，同时仍能在真实连接死亡时及时重连。
const HEARTBEAT_TIMEOUT = 45_000
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
  private _disposed: boolean = false

  private _connectionTimeoutTimer: ReturnType<typeof setTimeout> | null = null

  /**
   * 建立全局 WS 连接（登录后调用一次）
   *
   * BUG-FIX-fix_20260527_ws_dup_connect:
   * 问题根因: connect() 使用 50ms setTimeout 延迟实际连接，导致幂等检查失效。
   *          React StrictMode 下 useEffect 执行两次，两次 connect() 调用都在 setTimeout
   *          触发前通过检查，最终建立两条 WebSocket 连接，触发后端踢掉旧连接的连锁反应，
   *          导致 _status 事件频繁触发、组件级联重渲染、页面卡顿。
   * 修复方案: 移除 setTimeout 延迟，直接同步调用 _doConnect()，使幂等检查（connecting 状态判断）
   *          在同一事件循环内生效，确保相同 token 的重复调用被正确拦截。
   * 影响范围: GlobalWebSocket 连接建立流程
   * 修复日期: 2026-05-27
   */
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
        // BUG-FIX-M03: WS handler 层 console 残留
        // 问题根因: 连接级异常用 console.warn 记录。
        // 修复方案: 改用正式 logger.warn（_wsLogger），生产环境统一记录。
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
        // 延迟追踪：后端在 payload 注入 __send_ts（epoch ms），
        // 这里算"收到时刻 - 发送时刻"。忽略心跳/连接确认等无业务语义事件。
        const sendTs = data.__send_ts ?? data.data?.__send_ts
        if (typeof sendTs === 'number' && data.type && data.type !== 'heartbeat_ack') {
          const recvTs = Date.now()
          const latency = recvTs - sendTs
          const traceId =
            (data.data?.message_id as string)?.slice(0, 12) ||
            (data.data?.request_id as string)?.slice(0, 12) ||
            'null'
          // >500ms 视为异常延迟，用 warn 突出；正常用 debug 避免刷屏
          if (latency > 500) {
            _wsLogger.warn(
              `[WS_TRACE] <<< RECV type=${data.type} id=${traceId} latency=${latency}ms (⚠️异常)`,
            )
          } else {
            _wsLogger.debug(
              `[WS_TRACE] <<< RECV type=${data.type} id=${traceId} latency=${latency}ms`,
            )
          }
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
        // BUG-FIX-fix_20260624_ws_reconnect_dead_loop:
        // 后端在 token 无效/过期时以 code=4001 关闭连接（见 app_factory.py:244/248）。
        // 把 close code 传给重连逻辑：4001 = 认证被拒，需先刷新 token 再连；
        // 其他 code（网络断开、心跳超时等）= 正常重连，无需触碰 token。
        //
        // BUG-FIX-fix_20260625_ws_handshake_close_code_lost:
        // 后端旧实现在 accept() 前 close(4001)，浏览器拿到的是 HTTP 403 + close code 1006
        // 而非 4001，导致认证拒绝被误判为普通断连。后端已改为 accept() 后 close(4001)，
        // 正常情况下前端能收到 4001。但某些代理/网关可能吞掉 close code 导致 1006，
        // 故对 1006 + 从未连接过 + 本地 token 确实已过期 三者同时成立时，
        // 也视为认证拒绝。token 未过期时（如服务端宕机）仍走普通指数退避。
        let authRejected = event.code === 4001
        if (!authRejected && event.code === 1006 && !wasConnected) {
          // 仅当本地判定 token 已过期时才怀疑认证拒绝
          const { checkTokenExpiration } = useAuthStore.getState()
          authRejected = checkTokenExpiration()
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
          _wsLogger.warn('[GlobalWS] 心跳超时，连接可能已断开，主动关闭重连')
          if (this.ws) {
            // BUG-FIX-fix_20260624_ws_reconnect_dead_loop:
            // 心跳超时用 code=2002（TIMEOUT），**绝不复用 4001**。
            // 4001 已被后端用于「token 无效/过期」的认证拒绝（见 app_factory.py:244/248），
            // onclose 据此触发 token 刷新路径。若心跳超时也用 4001，会被误判为认证拒绝，
            // 在无 refresh token 的环境（测试/未登录）反复抛错。心跳超时属于网络层故障，
            // 应走普通重连（直接用当前 token 重连），不触发刷新。
            this.ws.close(2002, '心跳超时')
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

  /**
   * 调度重连
   *
   * @param authRejected 后端是否因认证问题关闭连接（close code === 4001）。
   *   - true：需先刷新 token 再连；刷新真失效则登出并停止重连。
   *   - false（默认）：网络/心跳超时等普通断连，直接用当前 token 重连。
   *
   * BUG-FIX-fix_20260624_ws_reconnect_dead_loop:
   * 问题根因: token 过期 → WS 握手被后端以 code=4001 关闭（客户端看到 HTTP 403）
   *   → onclose → _scheduleReconnect → 检测过期 → refreshToken。若刷新失败（含
   *   refresh_token 被 401 单次轮换击穿），旧实现 catch 块**用同一个过期 token 继续重连**
   *   → 后端再 4001 → 再重连…死循环，连接永远建不起来 → 后端推送全部丢失。
   *   这就是「推送不了」的根因。
   * 修复方案: 用 close code 精确区分认证拒绝与普通断连；认证拒绝时必须刷新成功才连，
   *   失败按错误类型分流：
   *   - 真认证失效（refresh_token 被 401/403 拒绝）→ triggerAuthExpired 走登出，停止重连
   *     （没有有效 token，连了也是 4001）。
   *   - 瞬时故障（网络/超时/5xx）→ 不连、不登出，按指数退避等下一轮（下次再尝试刷新），
   *     **绝不拿已知过期的旧 token 去连**，从源头切断 4001 死循环。
   */
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
