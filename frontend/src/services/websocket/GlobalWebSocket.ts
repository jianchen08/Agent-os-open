/** 全局单连接 WebSocket 服务 设计原则： */

import { buildGlobalWebSocketUrl } from '@/constants/websocket'
import { triggerAuthExpired } from '@/services/authCallbacks'
import { isAuthFailureFromError, isExpired, refresh, getAccessToken } from '@/services/auth/tokenLifecycle'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { loggers } from '@/utils/logger'

const _wsLogger = loggers.websocket

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'reconnecting'

interface PendingMessage {
  type: string
  [key: string]: unknown
}

/** 内部事件处理器存储类型（消息体运行时为任意 JSON，具体类型由订阅方泛型声明） */
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

/**
 * user_input 离线排队 TTL：入队后超过此时长仍未随重连发出，则从队列剔除并
 * 广播 user_input_send_timeout（UI 层据此移除"思考中"占位气泡并向用户报错）。
 * 发送层必须有界失败：token 过期致 WS 断连期间，消息不得静默滞留内存队列
 * （气泡无限转、刷新后凭空消失、全程零提示）。
 */
const USER_INPUT_QUEUE_TTL_MS = 20_000

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
  /**
   * 正在等待 token 刷新后重连。
   *
   * 4001（token 过期）触发的重连会先 refreshToken 再连。在 refresh 进行期间，
   * 外部（router.tsx 的 useEffect、useRealtimeEvents 的 visibilitychange 等）
   * 若用过期 token 调 connect()，会经 _clearTimers() 清掉退避计时器、并用旧
   * token 硬连 → 又 4001 → 重排退避 → 又被打断，形成死循环，refresh 永远执行
   * 不到（后端日志表现为稳定的每 ~5s 一次 4001，无 /auth/refresh）。
   *
   * 置 true 后 connect() 直接 return，保证 refresh 流程不被打断；refresh 完成
   * （成功或失败）后在 _scheduleReconnect 的回调里复位。
   */
  private _refreshingForReconnect: boolean = false
  /**
   * 被 4000 踢旧标记：本页连接被同一账号的新连接替换（B10 单连接）。
   *
   * 内核踢旧会发带 4000 状态码的 Close 帧；onclose(4000) 置位后，任何自动
   * 重连路径（visibilitychange 回前台、router token 变化等）都不得再 connect
   * ——否则 A/B 两页互相踢旧重连形成互踢环（双客户端风暴的残余
   * 触发源）。刷新页面（新模块实例）或登出（disconnect 复位）后恢复。
   */
  private _kickedByReplacement: boolean = false
  /** 断线前已确认的最大消息序号（用于断线补漏 last_sequence） */
  private _lastSequence: number = 0

  private _connectionTimeoutTimer: ReturnType<typeof setTimeout> | null = null

  /** user_input 排队超时计时器（key = client_message_id） */
  private _userInputTimers: Map<string, ReturnType<typeof setTimeout>> = new Map()

  /** 是否发起过至少一次连接（connect 被调用过）。刷新后 token 恢复期为 false：
   *  「从未连接」≠「断开」，连接状态映射据此区分首连中与真断开（不出误导横幅）。 */
  private _hasAttemptedConnect = false

  get hasAttemptedConnect(): boolean {
    return this._hasAttemptedConnect
  }

  /** 本页是否被新连接替换（4000 踢旧）：true 时自动路径不得重连。 */
  wasKickedByReplacement(): boolean {
    return this._kickedByReplacement
  }

  /** 建立全局 WS 连接（登录后调用一次） */
  connect(token: string): void {
    this._hasAttemptedConnect = true
    if (this._disposed) return
    // 正在等 token 刷新重连时，拒绝外部 connect（通常是用过期 token 的抢占调用）。
    // 否则会 _clearTimers 清掉 refresh 退避、用过期 token 硬连 → 4001 死循环，
    // refresh 永远执行不到。refresh 流程会在回调里自行 connect(新token)。
    if (this._refreshingForReconnect) {
      _wsLogger.debug('[GlobalWS] 正在刷新 token 重连，跳过外部 connect（避免用过期 token 打断 refresh）')
      return
    }
    // 被 4000 踢旧（本页已被其他连接替换）：禁止自动重连，否则会反过来踢掉
    // 当前持有者，形成互踢环。用户刷新页面（新实例）或登出（disconnect 复位）后恢复。
    if (this._kickedByReplacement) {
      _wsLogger.debug('[GlobalWS] 本页被新连接替换(code=4000)，跳过自动重连（刷新页面可恢复）')
      return
    }
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

    // 断线重连时带上 last_sequence，让后端重放断线期间的消息
    const url = buildGlobalWebSocketUrl(this._token, this._lastSequence > 0 ? this._lastSequence : undefined)
    _wsLogger.debug('[GlobalWS] connecting to %s (last_sequence=%d)', url.substring(0, 60), this._lastSequence)
    this.ws = new WebSocket(url)

    this._connectionTimeoutTimer = setTimeout(() => {
      if (this._status === 'connecting') {
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
      // 真实连接建立成功：清除被踢标记（踢旧后用户刷新/重登的恢复点）
      this._kickedByReplacement = false
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
        // 追踪 last_sequence：从消息中提取 sequence 字段，更新最大已知序号。
        // 后端两种 sequence 位置并存，共享同一全局 sequence 空间（ADR §3.5 第7条）：
        // - 流式族：data.data.sequence（嵌套）
        // - widget_event 族：data.sequence（顶层）
        const seqCandidates = [
          data?.data?.sequence,
          data?.sequence,
        ].filter((s) => typeof s === 'number') as number[]
        if (seqCandidates.length > 0) {
          const maxSeq = Math.max(...seqCandidates)
          if (maxSeq > this._lastSequence) {
            this._lastSequence = maxSeq
          }
        }
        // 处理 resync_required 事件：后端告知需要全量重新同步
        if (data.type === 'resync_required') {
          _wsLogger.warn('[GlobalWS] 收到 resync_required，触发全量消息重同步')
          this._emit('resync_required', data)
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
        this._kickedByReplacement = true
        // 被踢页面已永久失联：滞留队列的消息永远不会发出（连接不再重建），
        // 立即清空并撤销其排队超时计时，再广播 kicked 让 UI 明示用户——
        // 否则静默装死，用户以为页面在线，实际消息全部黑洞。
        this._queue = []
        this._userInputTimers.forEach((timer) => clearTimeout(timer))
        this._userInputTimers.clear()
        console.info('[GlobalWS] 被新连接替换(code=4000)，跳过重连')
        this._emit('kicked_by_replacement', {
          type: 'kicked_by_replacement',
          data: { reason: '本页连接已被同账号的其他页面替换' },
        })
        return
      }

      if (!this._disposed) {
        // 后端 token 无效/过期时以 code=4001 关闭连接，前端需先刷新 token 再重连。
        // 任何掉线（含 1006、心跳超时 2002、4001）都先检查 token 是否已过期：
        // 已建立连接掉了（wasConnected=true）也可能是 token 过期后才掉，旧逻辑的
        // !wasConnected 门控会让此类掉线用过期 token 硬连 → 4001 → 崩溃。isExpired
        // 只在真过期时返回 true，未过期时不触发刷新，安全。
        let authRejected = event.code === 4001
        if (!authRejected) {
          authRejected = isExpired()
        }
        this._scheduleReconnect(authRejected)
      }
    }
  }

  /** 断开连接（登出时调用） */
  disconnect(): void {
    this._disposed = true
    this._refreshingForReconnect = false
    this._kickedByReplacement = false
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
    this._userInputTimers.forEach((timer) => clearTimeout(timer))
    this._userInputTimers.clear()
    this._handlers.clear()
  }

  sendUserInput(threadId: string, content: string, opts?: {
    pipelineId?: string
    attachments?: unknown[]
    enableThinking?: boolean
    /** 思考强度（off/low/medium/high；内核透传 → llm_core 路由模型参数） */
    thinkingStrength?: 'off' | 'low' | 'medium' | 'high'
    clientMessageId?: string
  }): void {
    const msg: PendingMessage = {
      type: 'user_input',
      thread_id: threadId,
      content,
      pipeline_id: opts?.pipelineId || '',
      attachments: opts?.attachments || [],
      enable_thinking: opts?.enableThinking || false,
      thinking_strength: opts?.thinkingStrength || '',
      client_message_id: opts?.clientMessageId || '',
    }

    this._send(msg)

    // 错误透传（有界失败）：_send 对断线是静默入队（永不抛错），入队即挂 TTL——
    // 超时仍未发出则剔除并广播，UI 层撤占位气泡并提示用户，杜绝"无限思考中"。
    const cmid = (msg as { client_message_id?: string }).client_message_id
    if (cmid && this._isQueuedUserInput(cmid)) {
      this._armUserInputTimeout(cmid)
    }
  }

  /** 队列中是否存在指定 client_message_id 的 user_input */
  private _isQueuedUserInput(cmid: string): boolean {
    return this._queue.some(
      (m) => m.type === 'user_input' && (m as { client_message_id?: string }).client_message_id === cmid,
    )
  }

  /** 为排队中的 user_input 挂超时：到点仍在队列 → 剔除 + 广播 user_input_send_timeout */
  private _armUserInputTimeout(cmid: string): void {
    if (this._userInputTimers.has(cmid)) return
    const timer = setTimeout(() => {
      this._userInputTimers.delete(cmid)
      const idx = this._queue.findIndex(
        (m) =>
          m.type === 'user_input'
          && (m as { client_message_id?: string }).client_message_id === cmid,
      )
      if (idx === -1) return // 已随重连成功发出（或被去重剔除），无需处理
      const [dropped] = this._queue.splice(idx, 1)
      _wsLogger.warn(
        '[GlobalWS] user_input 排队 %dms 未发出（连接未恢复），丢弃并广播 send_timeout: cmid=%s',
        USER_INPUT_QUEUE_TTL_MS,
        cmid.slice(0, 8),
      )
      this._emit('user_input_send_timeout', {
        type: 'user_input_send_timeout',
        data: {
          thread_id: dropped.thread_id,
          pipeline_id: dropped.pipeline_id,
          client_message_id: cmid,
          content: dropped.content,
          reason: `连接断开超过 ${USER_INPUT_QUEUE_TTL_MS / 1000}s，消息未送达已撤回`,
        },
      })
    }, USER_INPUT_QUEUE_TTL_MS)
    this._userInputTimers.set(cmid, timer)
  }

  /**
   * 上报当前选中的会话切换（排队优先级键，[来源: docs/decisions/2026-08-15-pipeline-run-chain-serialization.md]）。
   * 内核据此把该用户的活跃管道更新为当前选中管道——全局并发闸门有排队时，
   * 活跃管道的 run 优先获得槽位。通知性消息：离线时直接丢弃（后端以最近
   * user_input 派发兜底），不进离线队列。
   */
  sendActiveThread(threadId: string, pipelineId?: string): void {
    if (this._status !== 'connected') return
    this._send({ type: 'active_thread_changed', thread_id: threadId, pipeline_id: pipelineId || '' })
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

  /** 订阅事件（handler 入参类型由调用方按事件契约声明） */
  subscribe<T = any>(event: string, handler: (data: T) => void): void {
    if (!this._handlers.has(event)) {
      this._handlers.set(event, new Set())
    }
    this._handlers.get(event)!.add(handler as EventHandler)
  }

  /** 取消订阅 */
  unsubscribe<T = any>(event: string, handler: (data: T) => void): void {
    this._handlers.get(event)?.delete(handler as EventHandler)
  }

  /** 获取当前连接状态 */
  get status(): ConnectionStatus {
    return this._status
  }

  /** 获取断线前已确认的最大消息序号 */
  get lastSequence(): number {
    return this._lastSequence
  }

  /** 手动设置 last_sequence（例如从 pipelineMessageStore 恢复游标时） */
  setLastSequence(seq: number): void {
    if (seq > this._lastSequence) {
      this._lastSequence = seq
    }
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
        // 已成功送入连接：撤销其排队超时计时（若有）
        if (msg.type === 'user_input') {
          const cmid = (msg as { client_message_id?: string }).client_message_id
          const timer = cmid ? this._userInputTimers.get(cmid) : undefined
          if (cmid && timer) {
            clearTimeout(timer)
            this._userInputTimers.delete(cmid)
          }
        }
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
          // 避免局域网抖动/后端繁忙时的误断。
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
    // 认证拒绝需先 refresh：置标志，防止退避期间外部 connect(oldToken) 打断 refresh
    if (authRejected) {
      this._refreshingForReconnect = true
    }
    this._reconnectTimer = setTimeout(async () => {
      if (this._disposed || !this._token) {
        this._refreshingForReconnect = false
        return
      }

      // 普通断连（非认证拒绝）：直接重连，不触碰 token
      if (!authRejected) {
        this.connect(this._token)
        return
      }

      // 认证拒绝：必须先刷新 token 再连（tokenLifecycle 唯一刷新源）
      _wsLogger.info('[GlobalWS] 连接被认证拒绝(4001)，刷新 token 后再重连')
      try {
        await refresh()
        // 刷新成功：用新 token 重连（refresh 已更新 localStorage 并通知 authStore 同步）
        const newToken = getAccessToken()
        if (newToken && newToken !== this._token) {
          this._token = newToken
          _wsLogger.info('[GlobalWS] Token 已刷新，用新 token 重连')
        }
        // 复位标志后 connect（connect 内部会检查此标志）
        this._refreshingForReconnect = false
        this.connect(this._token)
      } catch (refreshError) {
        this._refreshingForReconnect = false
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
