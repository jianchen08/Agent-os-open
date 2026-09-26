/**
 * WebSocket相关常量定义
 *
 * 与后端WebSocket端点对齐，确保前后端一致性。
 * Requirements: 4.1, 4.2
 */

import { API_BASE_URL } from './api'

export enum WebSocketStatus {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
}

/**
 * 打包件（Electron 自定义协议 app://）下内核的固定回源地址。
 *
 * app:// 页面的 location 宿主是自定义协议而非内核，WS 无法从 location 派生；
 * 装机版内核默认监听 127.0.0.1:9101（与 electron/kernel-manager.ts 的
 * KERNEL_DEFAULT_PORT 一致，与 dev 栈 9100 错峰；改动两处须同刀同步）。
 */
const PACKAGED_KERNEL_WS_ORIGIN = 'ws://127.0.0.1:9101'

/**
 * 从 API_BASE_URL 派生 WebSocket URL
 * http://localhost:8988 -> ws://localhost:8988
 * https://example.com -> wss://example.com
 * 空字符串 -> 从当前页面 location 派生（适用于 Vite 代理模式）
 *
 * loc 参数注入便于单测（jsdom 的 window.location 不可伪造）。
 */
export function deriveWsUrl(apiUrl: string, loc: Location = window.location): string {
  if (!apiUrl) {
    if (loc.protocol === 'app:') {
      return PACKAGED_KERNEL_WS_ORIGIN
    }
    const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${loc.host}`
  }
  const url = new URL(apiUrl)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.origin
}

/**
 * WebSocket服务器URL（从 API_BASE_URL 派生，或从环境变量读取）
 */
const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || deriveWsUrl(API_BASE_URL)

// ---- 协议版本 ----

/**
 * 客户端协议版本号
 *
 * 与后端 PROTOCOL_VERSION 保持一致，用于版本协商。
 */
const PROTOCOL_VERSION = '3.0.0'

/**
 * WS 握手凭据参数。
 *
 * 生产路径：`ticket`（POST /api/v1/ws-ticket 签发的一次性票据，单次消费 60s TTL）。
 * 兼容路径：`token`（JWT 直连；内核 ?token= 路径并存保留，供测试/外部脚本使用，
 * 前端生产流程不再传 token）。两者同时给出时 ticket 优先。
 */
export interface WsHandshakeAuth {
  ticket?: string
  token?: string
}

/**
 * 构建全局 WebSocket 连接 URL（不带 thread_id）
 *
 * 用于 GlobalWebSocketService 建立 /ws/chat 全局连接。
 * 断线重连时传入 last_sequence 让后端重放断线期间的消息。
 *
 * @param auth - 握手凭据（ticket 优先；token 为兼容路径）
 * @param lastSequence - 断线前已确认的最大消息序号（可选，用于断线补漏）
 * @returns 完整的 WebSocket URL
 */
export const buildGlobalWebSocketUrl = (auth: WsHandshakeAuth, lastSequence?: number): string => {
  const credential = auth.ticket
    ? `ticket=${encodeURIComponent(auth.ticket)}`
    : `token=${encodeURIComponent(auth.token ?? '')}`
  const base = `${WS_BASE_URL}/ws/chat?${credential}&version=${encodeURIComponent(PROTOCOL_VERSION)}`
  if (lastSequence != null && lastSequence > 0) {
    return `${base}&last_sequence=${lastSequence}`
  }
  return base
}

/**
 * WebSocket服务端事件类型（前端侧单一真值表）
 *
 * 收录原则（P2-3 单一真值源）：只收录后端（内核 ws_session/capability_router/
 * chat_send_handler 事件族 + 插件 event-bus.emit 全集）有发射源的事件名；
 * 与内核的对账由 wsEventNames.contract.test 机械执行（内核改名/删事件即红）。
 * 旧流式事件名已退役（LLM 流式服务契约 2026-08-26 定稿，DSH 8 事件协议）：
 * stream_chunk / thinking_start / thinking_chunk / thinking_end / stream_keepalive
 * 后端 llm_service 不再发射；前端 handler 对应删除（不留兼容层）。
 */
export const WS_SERVER_EVENTS = {
  /** 连接确认 */
  CONNECTION_CONFIRMATION: 'connection_confirmation',
  /** 任务完成 */
  TASK_COMPLETED: 'task_completed',
  /** 任务取消 */
  TASK_CANCELLED: 'task_cancelled',
  /** 任务状态实时更新（后端推送路径暂静默跳过，待 SDK frontend.emit 落地恢复） */
  TASK_STATUS_UPDATE: 'task_status_update',
  /** 任务状态变更（实时推送，同上待后端恢复） */
  TASK_STATUS_CHANGED: 'task_status_changed',
  /** 任务删除 */
  TASK_DELETED: 'task_deleted',
  /** 心跳响应（后端发送 heartbeat_ack） */
  HEARTBEAT: 'heartbeat_ack',
  /** 新消息 */
  NEW_MESSAGE: 'new_message',
  /** 流式输出开始 */
  STREAM_START: 'stream_start',
  /** 流式块开始（LLM 流式 8 事件协议：index=块索引，block_type=text/reasoning/tool-call） */
  BLOCK_START: 'block_start',
  /** 正文增量（LLM 流式 8 事件协议：按块索引归组追加） */
  TEXT_DELTA: 'text_delta',
  /** 思考增量（LLM 流式 8 事件协议：思考块起止由 block_start/block_end 表达） */
  REASONING_DELTA: 'reasoning_delta',
  /** 工具调用增量（LLM 流式 8 事件协议：arguments_delta 原始 JSON 串按块索引累积） */
  TOOL_CALL_DELTA: 'tool_call_delta',
  /** 块闭合（LLM 流式 8 事件协议：block 携带该块累积完整内容） */
  BLOCK_END: 'block_end',
  /** 用量（LLM 流式 8 事件协议：finish 前发出） */
  USAGE_EVENT: 'usage',
  /** 流式终结（LLM 流式 8 事件协议：reason=stop/length/tool_calls/error；断流由调用方补发 error） */
  FINISH: 'finish',
  /** 流式保活（LLM 流式 8 事件协议：超时探活，无业务载荷，前端无订阅消费面） */
  KEEPALIVE: 'keepalive',
  /** 流式输出结束（轮级：一轮 = 一条消息，不代表整次执行结束） */
  STREAM_END: 'stream_end',
  /** run 级收尾：一次用户输入触发的整次执行结束（生成态终止信号） */
  PIPELINE_ROUND_FINISHED: 'pipeline_round_finished',
  /** 流式输出错误（LLM 调用失败等） */
  STREAM_ERROR: 'stream_error',
  /** 插件执行错误（非终止信号：引擎 warn+继续的插件失败，只弹通知不标记消息失败） */
  PLUGIN_ERROR: 'plugin_error',
  /** 工具调用开始（管道流式事件） */
  TOOL_START: 'tool_start',
  /** 工具调用结果（管道流式事件） */
  TOOL_RESULT: 'tool_result',
  /** 工具执行进度（bash 等长任务执行中的 stdout 增量，task_observability 任务 2） */
  TOOL_PROGRESS: 'tool_progress',
  /** 人类交互超时提醒 */
  INTERACTION_TIMEOUT_REMINDER: 'interaction_timeout_reminder',
  /** 人类交互请求 */
  INTERACTION_REQUEST: 'interaction_request',
  /** 系统通知（chat.send_message 后台派发失败补报，统一错误模型） */
  SYSTEM_NOTIFICATION: 'system_notification',
  /** 迭代事件（管道引擎迭代开始/结束） */
  ITERATION: 'iteration',
  /** 会话成本更新（Token 用量变化时推送） */
  COST_UPDATE: 'cost_update',
  /** pending 输入队列同步（ADR-2026-08-26：入队/消费/修改/删除时推全量列表） */
  PENDING_INPUTS_CHANGED: 'pending_inputs_changed',
  /** 上下文压缩彻底失败（context_window_guard 经 frontend.emit 透传，按故障周期去重） */
  COMPRESSION_FAILED: 'compression_failed',
  /**
   * 压缩波次完成（context_window_guard 压缩成功时经 frontend.emit 透传）。
   * 压缩原地重写 message_slots 槽位 → 内容寻址指纹变异，前端跨压缩持有的
   * recordId 过期——收到后须按 API 权威全量对账刷新（BUG-72 A1）。
   */
  COMPRESSION_APPLIED: 'compression_applied',
  /** 段激活 ack（消息段模型：‹i/n› 多代切换后服务端后缀整段替换完成的对账事件，
   *  [来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md §5.1]；
   *  收到后对该 pipeline 增量重拉消息并刷新段清单） */
  SEGMENT_ACTIVATED: 'segment_activated',
  /** 需要全量重新同步（断线重连后后端告知 last_sequence 过期） */
  RESYNC_REQUIRED: 'resync_required',
  /** widget 事件（内核 PluginWidgetBroadcaster 周期快照 + 插件 widget 交互，ADR §3.5'） */
  WIDGET_EVENT: 'widget_event',
  /** 宿主选中引用变化（pipeline_host_context 插件转发外部宿主推送） */
  HOST_SELECTION_CHANGED: 'host_selection_changed',
} as const

/**
 * 前端本地服务事件（GlobalWebSocket 连接服务自有事件总线，非后端 WS 事件名）。
 * 发射端 = services/websocket/GlobalWebSocket；订阅端 = useRealtimeEvents /
 * streaming/index。与 WS_SERVER_EVENTS 分表，避免把本地事件误当后端协议。
 */
export const WS_LOCAL_EVENTS = {
  /** 连接（重）建立：streaming/useRealtimeEvents 据此做断线补漏 */
  RECONNECTED: 'reconnected',
  /** 连接状态机变迁（connected/reconnecting/disconnected；GlobalWebSocket 源头发射） */
  STATUS: '_status',
  /** 排队 user_input 超 TTL 未送达（撤占位气泡 + 原位错误消息） */
  USER_INPUT_SEND_TIMEOUT: 'user_input_send_timeout',
  /** 被同账号新连接替换（B10 单连接踢旧，Close code=4000，不自动重连） */
  KICKED_BY_REPLACEMENT: 'kicked_by_replacement',
} as const

/**
 * WebSocket关闭码（仅收录后端实际发送的应用层关闭码）
 *
 * CONNECTION_REPLACED 对齐内核 CLOSE_CODE_KICKED = 4000
 * （kernel/crates/session/src/auth.rs，B10 单连接踢旧）；前端据 4000 置位
 * 防重连标记，避免 A/B 双客户端互踢循环。
 */
export enum WebSocketErrorCode {
  /** 连接被新连接替换（内核踢旧两段式：kicked 文本帧先于 Close(4000)） */
  CONNECTION_REPLACED = 4000,
}
