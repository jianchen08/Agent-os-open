/** 全局单连接 WebSocket 服务 设计原则： */

import {
  buildGlobalWebSocketUrl,
  WS_LOCAL_EVENTS,
  WS_SERVER_EVENTS,
  WebSocketErrorCode,
} from '@/constants/websocket'
import { isAuthFailureFromError, isExpired, refresh, getAccessToken } from '@/services/auth/tokenLifecycle'
import { fetchWsTicket } from '@/services/auth/wsTicket'
import { triggerAuthExpired } from '@/services/authCallbacks'
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
const RECONNECT_MAX_DELAY = 30_000
const RECONNECT_MAX_RETRIES = 30
const HEARTBEAT_INTERVAL = 30_000
// 判死阈值 = pong 新鲜度上限：距最近一次心跳 ack 超过 90s（连续 3 个 30s 周期
// 无 ack）判定连接死亡。90s 而非单周期零容错：局域网/非本机访问（如跨设备
// ip=192.168.x.x）或后端繁忙时单次 ack 延迟常见，零容错（45s 一次超时就断）
// 会导致 WS 每 30-45s 反复断连，流式 chunk 大量丢失。判死截止期只由 ack 到达
// 刷新，周期 tick 只做检查、**不在 tick 上清掉重挂**——每 tick 重挂同一截止期
// 会让超时回调永远活不过下一跳（判死成死代码，僵尸连接上状态恒 connected、
// 重连不启，装机"内核未连接"横幅出现后无法自愈）。
const HEARTBEAT_TIMEOUT = 90_000
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
  /** 最近一次心跳 ack（pong）到达时刻：判死的唯一新鲜度来源 */
  private _lastPongAt: number = 0
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
   * 被 4000 踢旧标记：本页连接被超限踢旧替换（同账号连接数已满，最旧连接
   * 被替换——ADR 2026-10-01 多前端连接：配额内多端并存不踢，仅超限触发）。
   *
   * 内核超限踢旧会发带 4000 状态码的 Close 帧；onclose(4000) 置位后，任何自动
   * 重连路径（visibilitychange 回前台、router token 变化等）都不得再 connect
   * ——否则被踢端退避重连后再度超限被踢，形成循环（双客户端风暴的残余
   * 触发源）。刷新页面（新模块实例）或登出（disconnect 复位）后恢复。
   */
  private _kickedByReplacement: boolean = false
  /** 断线前已确认的最大消息序号（用于断线补漏 last_sequence） */
  private _lastSequence: number = 0

  private _connectionTimeoutTimer: ReturnType<typeof setTimeout> | null = null

  /**
   * user_input 在途超时计时器（key = client_message_id）。ADR 2026-09-29 回执
   * 契约：入队与直发两条路径恒挂——僵尸 TCP 上 ws.send() 不抛错不入队（帧静默
   * 进黑洞），计时器是唯一不依赖出站方向的有界失败机制。值携带 pipelineId/
   * threadId 供 ack(ok:false)/stream_start 撤销时构造事件载荷。
   */
  private _userInputTimers: Map<
    string,
    { timer: ReturnType<typeof setTimeout>; pipelineId: string; threadId: string }
  > = new Map()

  /** 是否发起过至少一次连接（connect 被调用过）。刷新后 token 恢复期为 false：
   *  「从未连接」≠「断开」，连接状态映射据此区分首连中与真断开（不出误导横幅）。 */
  private _hasAttemptedConnect = false

  /**
   * 连接代次号：connect 每次实际发起时递增。取票是异步窗口，期间可能被新
   * connect（换 token 重连）或 disconnect 取代——旧流程拿到票后据此自我作废，
   * 不产生孤儿连接（无代次校验时会双建连/互相覆盖连接超时计时器）。
   */
  private _connectSeq = 0

  get hasAttemptedConnect(): boolean {
    return this._hasAttemptedConnect
  }

  /** 本页是否被超限踢旧替换（4000 踢旧）：true 时自动路径不得重连。 */
  wasKickedByReplacement(): boolean {
    return this._kickedByReplacement
  }

  /**
   * 建立全局 WS 连接（登录后调用一次）。token 仅用于取票的认证状态与同 token
   * 去重/轮换记账：握手 URL 不携带 token，由 _doConnect 先取一次性票据再连。
   */
  connect(token: string): void {
    this._hasAttemptedConnect = true
    // connect 是「新会话开始」语义：复位登出置位的 _disposed。登出到下一次
    // connect 之间没有任何调用点（token 为 null 不触发 connect），复位不会
    // 引发幽灵重连；onclose / _scheduleReconnect 的 _disposed 守卫保持不变。
    this._disposed = false
    // 正在等 token 刷新重连时，拒绝外部 connect（通常是用过期 token 的抢占调用）。
    // 否则会 _clearTimers 清掉 refresh 退避、用过期 token 硬连 → 4001 死循环，
    // refresh 永远执行不到。refresh 流程会在回调里自行 connect(新token)。
    if (this._refreshingForReconnect) {
      _wsLogger.debug('[GlobalWS] 正在刷新 token 重连，跳过外部 connect（避免用过期 token 打断 refresh）')
      return
    }
    // 被 4000 踢旧（本页已被超限踢旧替换）：禁止自动重连，否则重连后再度
    // 超限又踢掉当前持有者，形成循环。用户刷新页面（新实例）或登出
    // （disconnect 复位）后恢复。
    if (this._kickedByReplacement) {
      _wsLogger.debug('[GlobalWS] 本页被超限踢旧(code=4000)，跳过自动重连（刷新页面可恢复）')
      return
    }
    if (this._status === 'connected') {
      if (this._token === token) return
      // 已连接时 token 轮换（主动续期/刷新）只记录新 token 供下次重连使用：
      // 连接本身不依赖 token 存活，拆掉健康连接重建会让流式传输中断
      // （last_sequence 补漏只是断线兜底，不构成拆连接的理由）。
      this._token = token
      return
    }
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

    this._connectSeq++
    void this._doConnect()
  }

  /**
   * 实际建立 WebSocket 连接：先 POST /api/v1/ws-ticket 取一次性票据，再用
   * ?ticket= 握手。取票失败（网络/401）不回退 token 直连，走既有重连退避；
   * 认证类失败与 4001 掉线同路径——先刷新 token，重连时再取新票。票据单次
   * 消费，重连（含 token 轮换后的重连）每次重新取票，不缓存复用。
   */
  private async _doConnect(): Promise<void> {
    if (this._disposed || this._status !== 'connecting') return
    const seq = this._connectSeq

    let ticket: string
    try {
      ticket = await fetchWsTicket()
    } catch (err) {
      // 取票 await 期间被新 connect 取代或已登出：本轮作废，不进入重连
      if (this._disposed || this._status !== 'connecting' || seq !== this._connectSeq) return
      // apiClient 统一错误信封：code 为 HTTP 状态字符串（401/403=认证拒绝）
      const status = (err as { code?: string } | null)?.code
      const authRejected = status === '401' || status === '403' || isExpired()
      _wsLogger.warn(
        '[GlobalWS] WS 票据获取失败（%s），进入重连退避: %s',
        authRejected ? '认证拒绝' : '非认证失败',
        err instanceof Error ? err.message : String(err),
      )
      this._scheduleReconnect(authRejected)
      return
    }
    // 取到票后再校验一次：await 窗口内被新 connect 取代 / 登出则丢弃本票
    if (this._disposed || this._status !== 'connecting' || seq !== this._connectSeq) return

    // 断线重连时带上 last_sequence，让后端重放断线期间的消息
    const url = buildGlobalWebSocketUrl({ ticket }, this._lastSequence > 0 ? this._lastSequence : undefined)
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
      this._emit(WS_LOCAL_EVENTS.STATUS, { status: 'connected' })
      this._emit('connect', { status: 'connected' })
      if (isReconnect) {
        this._emit(WS_LOCAL_EVENTS.RECONNECTED, { status: 'connected' })
      }
      useLayoutModeStore.getState().updateConnectionStatus({
        state: 'connected',
        lastConnectedAt: new Date().toISOString(),
        reconnectAttempt: 0,
      })
    }

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === WS_SERVER_EVENTS.HEARTBEAT) {
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
        // 应用层踢旧通知（内核超限踢旧两段式：kicked 文本帧先于 Close(4000)
        // 送达）。代理链可能吞掉 Close 帧状态码（浏览器端退化为 1006），文本帧
        // 先行送达即可置位防重连，避免被踢端重连后再超限循环——处置与
        // onclose(4000) 一致。
        if (data.type === 'kicked') {
          this._handleKickedByReplacement()
          return
        }
        // user_input 回执（ADR 2026-09-29）：ok:true 撤在途计时；ok:false 撤计时
        // 并立即透传既有 send_timeout 通道（serverAware=true，UI 文案按"服务器
        // 拒收"呈现，不再等 TTL 也不谎报"后端无记录"）。
        if (data.type === 'user_input_ack') {
          const cmid = typeof data.client_message_id === 'string' ? data.client_message_id : ''
          const armed = cmid ? this._disarmUserInputTimer(cmid) : undefined
          if (data.ok === false) {
            const reason = `服务器拒绝派发：${typeof data.error === 'string' ? data.error : '未知错误'}`
            _wsLogger.warn('[GlobalWS] user_input 被内核拒收: cmid=%s error=%s', cmid.slice(0, 8), data.error)
            this._emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {
              type: WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT,
              data: {
                thread_id: typeof data.thread_id === 'string' ? data.thread_id : (armed?.threadId ?? ''),
                pipeline_id: armed?.pipelineId ?? '',
                client_message_id: cmid,
                reason,
                serverAware: true,
              },
            })
          }
        }
        // stream_start：该管道已有轮次在跑（内核→前端方向实测可达），按 pipeline_id
        // 撤在途计时（回执契约的兜底撤销，ack 缺失时第二撤点）。
        if (data.type === 'stream_start') {
          const pid = data?.data?.pipeline_id ?? data?.pipeline_id
          if (typeof pid === 'string') {
            this._disarmUserInputTimersForPipeline(pid)
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
        // 非 JSON 帧忽略（豁免：底层 ping/探活等非 JSON 载荷属正常路径），
        // 仅 debug 留痕便于排查意外帧
        _wsLogger.debug('[WS_RAW] 收到非 JSON 帧，忽略')
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
      this._emit(WS_LOCAL_EVENTS.STATUS, { status: 'disconnected', code: event.code, reason: event.reason })
      useLayoutModeStore.getState().updateConnectionStatus({ state: 'disconnected' })

      if (event.code === WebSocketErrorCode.CONNECTION_REPLACED) {
        this._handleKickedByReplacement()
        return
      }

      // 应用层 kicked 帧已先行置位（Close 帧被代理吞掉状态码、浏览器端退化为
      // 1006 的场合）：本页已被超限踢旧替换，任何掉线路径都不得重连
      if (this._kickedByReplacement) {
        return
      }

      if (!this._disposed) {
        // 后端 token 无效/过期时以 code=4001 关闭连接，前端需先刷新 token 再重连。
        // 对所有掉线（含 1006、心跳超时 2002、4001）都先检查 token 是否过期再重连，
        // 不以「是否成功建立过连接」作门控：长连接存活期内 token 也可能过期才掉线，
        // 用过期 token 硬连会被后端再次以 4001 拒绝。isExpired 只在真过期时返回
        // true，未过期不会触发多余刷新。
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
    this._userInputTimers.forEach((armed) => clearTimeout(armed.timer))
    this._userInputTimers.clear()
    this._handlers.clear()
  }

  /**
   * 手动立即重连（断开横幅降级态「立即重连」按钮的唯一入口）：不等退避计时
   * 到点，用当前 token 立即发起一次连接尝试。用户显式介入解除 refresh 等待期
   * 对 connect 的封锁（该封锁只针对自动路径防 4001 打断刷新；tokenLifecycle
   * 的 refresh 单飞互斥，与在途刷新并发最终收敛于 connect 的幂等守卫）。
   * 无 token（未登录）时不动作。重连成功/失败后仍走既有自动重连状态机。
   */
  forceReconnect(): void {
    if (!this._token) return
    this._refreshingForReconnect = false
    this.connect(this._token)
  }

  sendUserInput(threadId: string, content: string, opts?: {
    pipelineId?: string
    attachments?: unknown[]
    enableThinking?: boolean
    /** 思考强度（off/low/medium/high；内核透传 → llm_core 路由模型参数） */
    thinkingStrength?: 'off' | 'low' | 'medium' | 'high'
    clientMessageId?: string
    /**
     * 消息级 execution_context（{workspace:{source_path,mode}, isolation:{level}}）：
     * 会话执行选项编辑后的最新值随身携带，内核 1a2 合并点优先于会话级注入。
     * 不带则后端按 thread metadata 出生值注入（与旧行为一致）。
     */
    executionContext?: Record<string, unknown>
    /**
     * 执行身份 agent 键：内核 route_user_input 提取后合成 {"agent.id": v} 单键
     * overlay 写管道 state 持久键（1c2f41915，消息级覆盖、后续轮次沿用）——
     * 模式会话的执行者绑定通道；附身不走此键（走 execution_context 人设键
     * [registry decl.persona.from 派生]，主 agent 全量工具语义）。不带则帧内
     * 无 agent_id 键。
     */
    agentId?: string
    /**
     * 执行管道配置（config/pipelines/ 登记名）：内核 route_user_input 透传
     * compiled_for 按需编译（2026-09-28 设计 D2 管道接通）——模式会话绑定
     * 专属管道的通道；不带则按会话默认管道。
     */
    pipelineConfigId?: string
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
      ...(opts?.executionContext ? { execution_context: opts.executionContext } : {}),
      ...(opts?.agentId ? { agent_id: opts.agentId } : {}),
      ...(opts?.pipelineConfigId ? { pipeline_config_id: opts.pipelineConfigId } : {}),
    }

    this._send(msg)

    // 错误透传（有界失败，恒挂）：入队路径超时未发出、直发路径超时未收回执
    // （user_input_ack/stream_start 到达即撤），都广播 user_input_send_timeout
    // 让 UI 撤占位气泡并提示——杜绝"无限思考中"（R302 实证直发零保护 8 分钟无声）。
    const cmid = (msg as { client_message_id?: string }).client_message_id
    if (cmid) {
      this._armUserInputTimeout(cmid, opts?.pipelineId || '', threadId)
    }
  }

  /** 为在途 user_input 挂超时：到点仍未被 ack 撤销 → 按在途形态广播 user_input_send_timeout */
  private _armUserInputTimeout(cmid: string, pipelineId: string, threadId: string): void {
    if (this._userInputTimers.has(cmid)) return
    const timer = setTimeout(() => {
      const armed = this._userInputTimers.get(cmid)
      this._userInputTimers.delete(cmid)
      const idx = this._queue.findIndex(
        (m) =>
          m.type === 'user_input'
          && (m as { client_message_id?: string }).client_message_id === cmid,
      )
      let reason: string
      let content: string | undefined
      if (idx !== -1) {
        // 仍在队列：断线未发出，剔除并广播（既有排队语义）
        const [dropped] = this._queue.splice(idx, 1)
        reason = `连接断开超过 ${USER_INPUT_QUEUE_TTL_MS / 1000}s，消息未送达已撤回`
        content = typeof dropped.content === 'string' ? dropped.content : undefined
        _wsLogger.warn(
          '[GlobalWS] user_input 排队 %dms 未发出（连接未恢复），丢弃并广播 send_timeout: cmid=%s',
          USER_INPUT_QUEUE_TTL_MS,
          cmid.slice(0, 8),
        )
      } else {
        // 已直发（帧过 ws.send）但 TTL 内无回执：连接可能已僵死，消息是否到达
        // 不可知——如实报"未收到服务器回执"，不代内核断言有无记录
        reason = `已发送但 ${USER_INPUT_QUEUE_TTL_MS / 1000}s 内未收到服务器回执（连接可能已中断）`
        _wsLogger.warn(
          '[GlobalWS] user_input 直发 %dms 未收到回执（连接疑似僵死），广播 send_timeout: cmid=%s',
          USER_INPUT_QUEUE_TTL_MS,
          cmid.slice(0, 8),
        )
      }
      this._emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {
        type: WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT,
        data: {
          thread_id: armed?.threadId ?? threadId,
          pipeline_id: armed?.pipelineId ?? pipelineId,
          client_message_id: cmid,
          ...(content !== undefined ? { content } : {}),
          reason,
        },
      })
    }, USER_INPUT_QUEUE_TTL_MS)
    this._userInputTimers.set(cmid, { timer, pipelineId, threadId })
  }

  /** ack 到达（ok 双值）即撤销在途计时器；返回撤销前的记载供 ok:false 透传构造事件 */
  private _disarmUserInputTimer(cmid: string):
    | { pipelineId: string; threadId: string }
    | undefined {
    const armed = this._userInputTimers.get(cmid)
    if (!armed) return undefined
    clearTimeout(armed.timer)
    this._userInputTimers.delete(cmid)
    return { pipelineId: armed.pipelineId, threadId: armed.threadId }
  }

  /** stream_start 到达：按 pipeline_id 撤销该管道全部在途计时（回执缺 cmid 时的兜底） */
  private _disarmUserInputTimersForPipeline(pipelineId: string): void {
    if (!pipelineId) return
    for (const [cmid, armed] of this._userInputTimers) {
      if (armed.pipelineId === pipelineId) {
        clearTimeout(armed.timer)
        this._userInputTimers.delete(cmid)
      }
    }
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

  /** 重新生成：截断到目标 user 消息后重跑（缺省最后一条 user；带 new_content
   *  为编辑重发——后端改写目标消息内容后重跑）。通知性消息：离线时入队，
   *  重连后随 _flushQueue 发出。 */
  sendRegenerate(threadId: string, opts?: {
    pipelineId?: string
    userMessageId?: string
    newContent?: string
  }): void {
    this._send({
      type: 'regenerate',
      thread_id: threadId,
      pipeline_id: opts?.pipelineId || '',
      user_message_id: opts?.userMessageId || '',
      new_content: opts?.newContent || '',
    })
  }

  /** 响应子 Agent 输入请求 */
  sendUserInputResponse(threadId: string, executionId: string, response: string): void {
    this._send({ type: 'user_input_response', thread_id: threadId, execution_id: executionId, response })
  }

  /**
   * 段激活：‹i/n› 多代切换（消息段模型 P1，方案 §5 写事件 3）。服务端按后缀
   * 语义整段替换并在 run 活跃时拒绝（错误「任务运行中」——调用方在发送前自持
   * streaming 预检 toast，ack 对账纠偏）。通知性消息：离线时入队，重连后随
   * _flushQueue 发出；segment_activated ack 由 useRealtimeEvents 防抖对账。
   */
  sendSegmentActivate(threadId: string, opts?: {
    pipelineId?: string
    segmentId?: string
  }): void {
    this._send({
      type: 'segment_activate',
      thread_id: threadId,
      pipeline_id: opts?.pipelineId || '',
      segment_id: opts?.segmentId || '',
    })
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
      _wsLogger.info(
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
        // flush 把消息从队列移入直发：在途计时保持原样——TTL 到点回调按
        // "不在队列"走"已发送未收回执"分支（ADR 2026-09-29：直发同样可能进
        // 僵尸 TCP，计时由 ack/stream_start/TTL 三点收口，flush 不算送达）。
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
        try {
          h(data)
        } catch (e) {
          // handler 异常不影响其他 handler，但零留痕会让“订阅了却没触发”无从排查
          _wsLogger.debug('[GlobalWS] handler 异常 event=%s error=%s', event, e)
        }
      }
    }
  }

  private _startHeartbeat(): void {
    this._stopHeartbeat()
    // 连接刚建立即视为新鲜：首个判死窗口从这里起算
    this._lastPongAt = Date.now()
    this._heartbeatTimer = setInterval(() => {
      if (this._status !== 'connected') return
      // pong 新鲜度判死：截止期 = lastPongAt + HEARTBEAT_TIMEOUT，只被 ack 到达
      // 刷新、不被 tick 重挂。连续 3 个周期收不到 ack（后端假死/僵尸 TCP/代理
      // 静默断连）在此触发 2002 主动关闭，经 onclose 走既有重连路径。
      if (Date.now() - this._lastPongAt >= HEARTBEAT_TIMEOUT) {
        _wsLogger.warn(
          '[GlobalWS] 已 %d s 未收到心跳 ack，判定连接死亡，主动关闭重连',
          HEARTBEAT_TIMEOUT / 1000,
        )
        // 判死即停心跳：close → onclose 之间（真实浏览器为异步窗口）不再重复
        // 判死（幂等），重连成功 onopen 后重挂。
        this._stopHeartbeat()
        if (this.ws) {
          // 心跳超时用 code=2002（TIMEOUT），**绝不复用 4001**。
          // 4001 已被后端用于「token 无效/过期」的认证拒绝（见 app_factory.py:244/248），
          // onclose 据此触发 token 刷新路径。若心跳超时也用 4001，会被误判为认证拒绝，
          // 在无 refresh token 的环境（测试/未登录）反复抛错。心跳超时属于网络层故障，
          // 应走普通重连（直接用当前 token 重连），不触发刷新。
          this.ws.close(2002, '心跳超时')
        }
        return
      }
      this._send({ type: 'heartbeat', timestamp: Date.now() })
    }, HEARTBEAT_INTERVAL)
  }

  private _handleHeartbeatAck(): void {
    this._lastPongAt = Date.now()
  }

  private _stopHeartbeat(): void {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer)
      this._heartbeatTimer = null
    }
  }

  /**
   * 踢旧处置（onclose(4000) 与应用层 kicked 帧两入口共用）：置位防重连、
   * 清空滞留队列、广播 kicked_by_replacement。幂等——kicked 帧与 Close(4000)
   * 先后到达时只处置一次。
   *
   * 多前端连接下仅超限触发（同账号连接数已满，LRU 踢最旧）；被踢页面已
   * 永久失联：滞留队列的消息永远不会发出（连接不再重建），立即清空并撤销
   * 其排队超时计时，再广播让 UI 明示用户——否则静默装死，用户以为页面在线，
   * 实际消息全部黑洞。
   */
  private _handleKickedByReplacement(): void {
    if (this._kickedByReplacement) return
    this._kickedByReplacement = true
    this._queue = []
    this._userInputTimers.forEach((armed) => clearTimeout(armed.timer))
    this._userInputTimers.clear()
    _wsLogger.info('[GlobalWS] 被超限踢旧（连接数已满），跳过重连')
    this._emit(WS_LOCAL_EVENTS.KICKED_BY_REPLACEMENT, {
      type: WS_LOCAL_EVENTS.KICKED_BY_REPLACEMENT,
      data: { reason: '连接数已满，最旧连接被替换' },
    })
  }

  /** 调度重连 - true：需先刷新 token 再连；刷新真失效则登出并停止重连。 */
  private _scheduleReconnect(authRejected: boolean = false): void {
    if (this._disposed) return

    let delay: number
    if (this._reconnectAttempts >= RECONNECT_MAX_RETRIES) {
      delay = RECONNECT_MAX_DELAY
      _wsLogger.info('[GlobalWS] 超过最大重连次数，改为 %dms 间隔持续重连', delay)
    } else {
      delay = Math.min(
        RECONNECT_BASE_DELAY * Math.pow(2, this._reconnectAttempts),
        RECONNECT_MAX_DELAY,
      )
    }
    this._reconnectAttempts++
    _wsLogger.info('[GlobalWS] %dms 后重连（第 %d 次, authRejected=%s）', delay, this._reconnectAttempts, authRejected)
    // 标记为重连中，更新 UI 状态：带上累计尝试次数，供断开横幅呈现
    // 「正在重连（第 N 次）」动态进度与降级阈值（RECONNECT_DEGRADE_ATTEMPTS）判定
    this._status = 'reconnecting'
    this._emit(WS_LOCAL_EVENTS.STATUS, { status: 'reconnecting' })
    useLayoutModeStore.getState().updateConnectionStatus({
      state: 'reconnecting',
      reconnectAttempt: this._reconnectAttempts,
    })
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
