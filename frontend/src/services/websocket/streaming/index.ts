/**
 * 全局流式事件服务（管道 ID 路由版本）
 *
 * 核心设计原则：
 * - pipeline_id 是唯一的路由键
 * - 所有消息操作统一通过 pipelineMessageStore
 * - 每个管道独立渲染，互不干扰
 * - 主管道是默认管道（无 pipeline_id 时用 session_id 充当）
 * - 后台会话的管道也能独立流式输出
 *
 * 超时机制：
 * - 只要收到任何非终止流式事件（chunk / thinking / tool / keepalive 等），
 *   自动重置该管道的 chunk 超时计时器，无需各 handler 手动调用。
 * - 终止事件（stream_end / stream_error / new_message）由对应 handler 自行清理。
 */
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'

import { clearAllChunkTimeouts, getChunkTimeoutMessageId, onChunkTimeout, resetChunkTimeout } from './chunkTimeout'
import {
  handleNewMessage,
  handleStreamChunk,
  handleStreamEnd,
  handleStreamError,
  handleStreamKeepalive,
  handleStreamStart,
  handleSubAgentCreated,
  handleThinkingChunk,
  handleThinkingEnd,
  handleThinkingStart,
  handleToolResult,
  handleToolStart,
  handleIteration,
} from './handlers'
import { handleChunkTimeout, handlePipelineReceived, handleReconnected, handleStateChange, handleSystemNotification } from './lifecycleHandlers'
import { resolvePipelineId } from './router'

/** 终止事件：这些事件由 handler 自行清理超时，不需要集中重置 */
const TERMINAL_EVENTS = new Set([
  WS_SERVER_EVENTS.STREAM_END,
  WS_SERVER_EVENTS.STREAM_ERROR,
  WS_SERVER_EVENTS.NEW_MESSAGE,
])

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}

/**
 * 为非终止事件包装集中式 chunk 超时重置
 *
 * 核心逻辑：只要有任何流式事件到达（chunk、thinking、tool、keepalive 等），
 * 就认为连接仍然活跃，重置 60 秒超时计时器。
 * 终止事件（stream_end / stream_error / new_message）由各自 handler 负责清理。
 */
function _wrapWithTimeoutReset(event: string, handler: (data: any) => void): (data: any) => void {
  if (TERMINAL_EVENTS.has(event)) {
    return handler
  }
  return (data: any) => {
    const pipelineId = resolvePipelineId(data)
    if (pipelineId) {
      const messageId = getChunkTimeoutMessageId(pipelineId) || data.message_id || data.data?.message_id
      if (messageId) {
        resetChunkTimeout(pipelineId, messageId)
      }
    }
    handler(data)
  }
}

/**
 * 全局 WS 事件日志包装器
 *
 * 记录每一个到达前端的 WS 事件类型、pipelineId、messageId，
 * 用于定位消息生命周期问题。
 */
function _logEvent(eventType: string, data: any): void {
  if (eventType === 'stream_chunk' || eventType === 'stream_keepalive' || eventType === 'thinking_chunk') return
  const pid = resolvePipelineId(data)
  const mid = data.message_id || data.data?.message_id || data.data?.id || ''
  const content = data.data?.content || data.content || ''
  console.warn(
    `[WS-EVENT] ${eventType.padEnd(22)} pid=${(pid?.slice(0, 12) || '-').padEnd(12)} mid=${(mid?.slice(0, 12) || '-').padEnd(12)} contentLen=${content.length}`,
  )
}

/**
 * 初始化全局流式事件处理器（幂等，重复调用安全）
 */
export function initStreamingEvents(): void {
  if (_initialized) return
  _initialized = true

  const _logWrap = (event: string, handler: (data: any) => void) => (data: any) => {
    _logEvent(event, data)
    handler(data)
  }

  _handlers[WS_SERVER_EVENTS.STREAM_START] = _logWrap(WS_SERVER_EVENTS.STREAM_START, _wrapWithTimeoutReset(WS_SERVER_EVENTS.STREAM_START, handleStreamStart))
  _handlers[WS_SERVER_EVENTS.STREAM_CHUNK] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk)
  _handlers[WS_SERVER_EVENTS.STREAM_END] = _logWrap(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd)
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = _logWrap(WS_SERVER_EVENTS.STREAM_ERROR, handleStreamError)
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = _logWrap(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)
  _handlers[WS_SERVER_EVENTS.THINKING_START] = _logWrap(WS_SERVER_EVENTS.THINKING_START, _wrapWithTimeoutReset(WS_SERVER_EVENTS.THINKING_START, handleThinkingStart))
  _handlers[WS_SERVER_EVENTS.THINKING_CHUNK] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.THINKING_CHUNK, handleThinkingChunk)
  _handlers[WS_SERVER_EVENTS.THINKING_END] = _logWrap(WS_SERVER_EVENTS.THINKING_END, _wrapWithTimeoutReset(WS_SERVER_EVENTS.THINKING_END, handleThinkingEnd))
  _handlers[WS_SERVER_EVENTS.TOOL_START] = _logWrap(WS_SERVER_EVENTS.TOOL_START, _wrapWithTimeoutReset(WS_SERVER_EVENTS.TOOL_START, handleToolStart))
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = _logWrap(WS_SERVER_EVENTS.TOOL_RESULT, _wrapWithTimeoutReset(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult))
  _handlers[WS_SERVER_EVENTS.SUB_AGENT_CREATED] = _logWrap(WS_SERVER_EVENTS.SUB_AGENT_CREATED, handleSubAgentCreated)
  _handlers[WS_SERVER_EVENTS.STREAM_KEEPALIVE] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.STREAM_KEEPALIVE, handleStreamKeepalive)
  _handlers[WS_SERVER_EVENTS.ITERATION] = _logWrap(WS_SERVER_EVENTS.ITERATION, _wrapWithTimeoutReset(WS_SERVER_EVENTS.ITERATION, handleIteration))

  _handlers[WS_SERVER_EVENTS.STATE_CHANGE] = _logWrap(WS_SERVER_EVENTS.STATE_CHANGE, handleStateChange)
  _handlers[WS_SERVER_EVENTS.PIPELINE_RECEIVED] = _logWrap(WS_SERVER_EVENTS.PIPELINE_RECEIVED, handlePipelineReceived)
  _handlers[WS_SERVER_EVENTS.SYSTEM_NOTIFICATION] = _logWrap(WS_SERVER_EVENTS.SYSTEM_NOTIFICATION, handleSystemNotification)

  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.subscribe(event, handler)
  }

  // FIX: WS 重连后对正在 streaming 的管道调用 fetchMessages 做断线补漏
  _handlers['reconnected'] = handleReconnected
  globalWS.subscribe('reconnected', _handlers['reconnected'])

  // FIX: chunk 超时时保留已累积内容，避免显示空白
  onChunkTimeout(handleChunkTimeout)
}

/**
 * 销毁全局流式事件处理器
 */
export function destroyStreamingEvents(): void {
  if (!_initialized) return
  clearAllChunkTimeouts()
  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.unsubscribe(event, handler)
  }
  Object.keys(_handlers).forEach((k) => delete _handlers[k])
  _initialized = false
}

/**
 * 重置初始化状态并重新注册（HMR 安全）
 * Vite HMR 热更新时，旧模块的 _initialized=true 会阻止新 handler 注册，
 * 调用此方法强制重新初始化。
 */
export function reinitStreamingEvents(): void {
  destroyStreamingEvents()
  initStreamingEvents()
}

if (import.meta.hot) {
  import.meta.hot.accept(() => {
    reinitStreamingEvents()
  })
  import.meta.hot.dispose(() => {
    destroyStreamingEvents()
  })
}
