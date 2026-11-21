/**
 * WebSocket相关常量定义
 *
 * 与后端WebSocket端点对齐，确保前后端一致性。
 * Requirements: 4.1, 4.2
 */

import { parseYamlStrict } from '@/services/schema/yaml'
import { API_BASE_URL } from './api'
// 打包件内核回源常量：生成物单源（scripts/generate_port_constants.py 真值区），
// 与 electron KERNEL_DEFAULT_PORT 同一真值，闸 scripts/check_kernel_port_sync.py 执法。
import { PACKAGED_KERNEL_HOST, PACKAGED_KERNEL_PORT } from './packagedKernelPort.generated'
// WS 事件名单单源（ADR 2026-10-10-ws-events-single-source）：Vite ?raw 内嵌
// 编译进产物（无 yaml 插件、无构建期生成物）；运行时 js-yaml 解析派生
// WS_SERVER_EVENTS。内核侧 include_str! 同读本文件——双侧各自解析同一
// 物理文件即真单源，事件名改动只改 yaml 一处。
import wsEventsSource from '../../../config/kernel/ws_events.yaml?raw'

export enum WebSocketStatus {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
}

/**
 * 打包件（Electron 自定义协议 app://）下内核的固定回源地址。
 *
 * app:// 页面的 location 宿主是自定义协议而非内核，WS 无法从 location 派生；
 * host/port 取自生成物单源（packagedKernelPort.generated.ts，由
 * scripts/generate_port_constants.py 真值区生成，与 electron
 * KERNEL_DEFAULT_PORT 同一真值；闸 scripts/check_kernel_port_sync.py 执法），
 * 与 dev 栈 9100 错峰（ADR 2026-09-20）。
 */
const PACKAGED_KERNEL_WS_ORIGIN = `ws://${PACKAGED_KERNEL_HOST}:${PACKAGED_KERNEL_PORT}`

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
export const PROTOCOL_VERSION = '3.0.0'

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
 * WS 服务端事件类型（事件名契约单源 = config/kernel/ws_events.yaml，
 * ADR 2026-10-10-ws-events-single-source）
 *
 * 收录原则：只收录后端（内核 ws_session/capability_router/chat_send_handler
 * 事件族 + 插件 event-bus.emit 全集）有发射源的事件名；事件名与逐条说明见
 * 单源文件（本表不再持有事件名字面量）。键表是前端常量词汇表的镜像（TS
 * 静态检查 `WS_SERVER_EVENTS.X` 拼写需要），与 yaml server_events 键集合在
 * 模块加载期 fail-closed 对账——集合不等即抛错，相关测试全部变红。
 * 旧流式事件名已退役（LLM 流式服务契约 2026-08-26 定稿，DSH 8 事件协议）：
 * stream_chunk / thinking_start / thinking_chunk / thinking_end / stream_keepalive
 * 后端 llm_service 不再发射；前端 handler 对应删除（不留兼容层）。
 */
const WS_SERVER_EVENT_KEYS = [
  'CONNECTION_CONFIRMATION',
  'TASK_COMPLETED',
  'TASK_CANCELLED',
  'TASK_FAILED',
  'TASK_STATUS_UPDATE',
  'TASK_STATUS_CHANGED',
  'TASK_DELETED',
  'TRIGGER_FIRED',
  'HEARTBEAT',
  'NEW_MESSAGE',
  'STREAM_START',
  'BLOCK_START',
  'TEXT_DELTA',
  'REASONING_DELTA',
  'TOOL_CALL_DELTA',
  'BLOCK_END',
  'USAGE_EVENT',
  'FINISH',
  'KEEPALIVE',
  'STREAM_END',
  'PIPELINE_ROUND_FINISHED',
  'RUN_FAILED',
  'STREAM_ERROR',
  'PLUGIN_ERROR',
  'TOOL_START',
  'TOOL_RESULT',
  'TOOL_PROGRESS',
  'INTERACTION_TIMEOUT_REMINDER',
  'INTERACTION_REQUEST',
  'NOTIFY_FLUSH',
  'SYSTEM_NOTIFICATION',
  'ITERATION',
  'COST_UPDATE',
  'PENDING_INPUTS_CHANGED',
  'COMPRESSION_FAILED',
  'COMPRESSION_STARTED',
  'COMPRESSION_ENDED',
  'COMPRESSION_APPLIED',
  'SEGMENT_ACTIVATED',
  'MESSAGES_TRUNCATED',
  'USER_MESSAGE_CONFIRMED',
  'RESYNC_REQUIRED',
  'WIDGET_EVENT',
  'HOST_SELECTION_CHANGED',
  'APPROVAL_TAKEN',
  'ROUTE_ERROR',
  'UI_COMMAND',
] as const

type WsServerEventKey = (typeof WS_SERVER_EVENT_KEYS)[number]

export type WsServerEvents = Readonly<Record<WsServerEventKey, string>>

/**
 * 解析单源 yaml 文本并派生 server_events 名单（fail-closed 对账）：
 * 键集合与词汇表镜像逐一对齐（缺键/多键均抛错），值必须是非空字符串。
 * 解析失败不回落空对象——契约损坏必须显式可见。source 注入为纯函数
 * （单测喂好/坏文档覆盖全部分支；生产以 ?raw 内嵌名单调用，模块加载期执行）。
 */
export function deriveWsServerEventsFromSource(source: string): WsServerEvents {
  const doc = parseYamlStrict(source) as {
    ws_events?: { server_events?: Record<string, unknown> }
  }
  const events = doc?.ws_events?.server_events
  if (!events || typeof events !== 'object') {
    throw new Error('ws_events.yaml 缺 ws_events.server_events 节（fail-closed）')
  }
  const expected = WS_SERVER_EVENT_KEYS as readonly string[]
  const missing = expected.filter((k) => !(k in events))
  const extra = Object.keys(events).filter((k) => !expected.includes(k))
  if (missing.length > 0 || extra.length > 0) {
    throw new Error(
      `WS 事件名单单源键集合与前端词汇表漂移（fail-closed）：missing=[${missing.join(', ')}] extra=[${extra.join(', ')}]`,
    )
  }
  for (const [key, value] of Object.entries(events)) {
    if (typeof value !== 'string' || value.length === 0) {
      throw new Error(`ws_events.yaml server_events.${key} 必须是非空字符串（fail-closed）`)
    }
  }
  return Object.freeze({ ...events }) as WsServerEvents
}

export const WS_SERVER_EVENTS: WsServerEvents = deriveWsServerEventsFromSource(wsEventsSource)

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
  /** 被超限踢旧（同账号连接数已满，最旧连接被替换；Close code=4000，不自动重连） */
  KICKED_BY_REPLACEMENT: 'kicked_by_replacement',
} as const

/**
 * 连续重连失败降级阈值：reconnectAttempt 达此次数后，断连通知从「正在重连
 * （第 N 次）」升级为「多次重连失败 + 立即重连」按钮（自动重连不停止，这是
 * 唯一允许用户干预的形态）。消费方 = LayoutAlertNotifications 的
 * useLayoutAlerts（通知中心承载）。
 */
export const RECONNECT_DEGRADE_ATTEMPTS = 10

/**
 * WebSocket关闭码（仅收录后端实际发送的应用层关闭码）
 *
 * CONNECTION_REPLACED 对齐内核 CLOSE_CODE_KICKED = 4000
 * （kernel/crates/session/src/auth.rs，超限踢旧：同账号连接数已满 LRU 踢最旧，
 * ADR 2026-10-01 多前端连接——配额内多端并存不踢）；前端据 4000 置位防重连
 * 标记，避免被踢端退避重连后再度超限被踢的循环。
 */
export enum WebSocketErrorCode {
  /** 连接被超限踢旧替换（内核两段式：kicked 文本帧先于 Close(4000)） */
  CONNECTION_REPLACED = 4000,
}
