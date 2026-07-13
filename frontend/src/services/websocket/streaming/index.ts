/** 全局流式事件服务（管道 ID 路由版本） 核心设计原则： */
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { loggers } from '@/utils/logger'

import {
  handleGlobalError,
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
import { handleCostUpdate, handleReconnected, handleStateChange, handleSystemNotification } from './lifecycleHandlers'
import { resolvePipelineId } from './router'

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}

/** 全局 WS 事件日志包装器 记录每一个到达前端的 WS 事件类型、pipelineId、messageId， */
function _logEvent(eventType: string, data: any): void {
  if (eventType === 'stream_chunk' || eventType === 'stream_keepalive' || eventType === 'thinking_chunk') return
  const pid = resolvePipelineId(data)
  const mid = data.message_id || data.data?.message_id || data.data?.id || ''
  const content = data.data?.content || data.content || ''
  loggers.websocket.debug(
    `[WS-EVENT] ${eventType.padEnd(22)} pid=${(pid?.slice(0, 12) || '-').padEnd(12)} mid=${(mid?.slice(0, 12) || '-').padEnd(12)} contentLen=${content.length}`,
  )
}

/** 初始化全局流式事件处理器（幂等，重复调用安全） */
export function initStreamingEvents(): void {
  if (_initialized) return
  _initialized = true

  const _logWrap = (event: string, handler: (data: any) => void) => (data: any) => {
    _logEvent(event, data)
    handler(data)
  }

  _handlers[WS_SERVER_EVENTS.STREAM_START] = _logWrap(WS_SERVER_EVENTS.STREAM_START, handleStreamStart)
  _handlers[WS_SERVER_EVENTS.STREAM_CHUNK] = _logWrap(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk)
  _handlers[WS_SERVER_EVENTS.STREAM_END] = _logWrap(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd)
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = _logWrap(WS_SERVER_EVENTS.STREAM_ERROR, handleStreamError)
  // -M01: 注册通用 ERROR 事件 handler
  _handlers[WS_SERVER_EVENTS.ERROR] = _logWrap(WS_SERVER_EVENTS.ERROR, handleGlobalError)
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = _logWrap(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)
  _handlers[WS_SERVER_EVENTS.THINKING_START] = _logWrap(WS_SERVER_EVENTS.THINKING_START, handleThinkingStart)
  _handlers[WS_SERVER_EVENTS.THINKING_CHUNK] = _logWrap(WS_SERVER_EVENTS.THINKING_CHUNK, handleThinkingChunk)
  _handlers[WS_SERVER_EVENTS.THINKING_END] = _logWrap(WS_SERVER_EVENTS.THINKING_END, handleThinkingEnd)
  _handlers[WS_SERVER_EVENTS.TOOL_START] = _logWrap(WS_SERVER_EVENTS.TOOL_START, handleToolStart)
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = _logWrap(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult)
  _handlers[WS_SERVER_EVENTS.SUB_AGENT_CREATED] = _logWrap(WS_SERVER_EVENTS.SUB_AGENT_CREATED, handleSubAgentCreated)
  _handlers[WS_SERVER_EVENTS.STREAM_KEEPALIVE] = _logWrap(WS_SERVER_EVENTS.STREAM_KEEPALIVE, handleStreamKeepalive)
  _handlers[WS_SERVER_EVENTS.ITERATION] = _logWrap(WS_SERVER_EVENTS.ITERATION, handleIteration)

  _handlers[WS_SERVER_EVENTS.STATE_CHANGE] = _logWrap(WS_SERVER_EVENTS.STATE_CHANGE, handleStateChange)
  _handlers[WS_SERVER_EVENTS.SYSTEM_NOTIFICATION] = _logWrap(WS_SERVER_EVENTS.SYSTEM_NOTIFICATION, handleSystemNotification)
  _handlers[WS_SERVER_EVENTS.COST_UPDATE] = _logWrap(WS_SERVER_EVENTS.COST_UPDATE, handleCostUpdate)

  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.subscribe(event, handler)
  }

  // WS 重连后对正在 streaming 的管道调用 fetchMessages 做断线补漏
  _handlers['reconnected'] = handleReconnected
  globalWS.subscribe('reconnected', _handlers['reconnected'])
}

/** 销毁全局流式事件处理器 */
export function destroyStreamingEvents(): void {
  if (!_initialized) return
  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.unsubscribe(event, handler)
  }
  Object.keys(_handlers).forEach((k) => delete _handlers[k])
  _initialized = false
}

/** 重新初始化流式事件处理器（销毁后重建） 用于以下场景： */
export function reinitStreamingEvents(): void {
  destroyStreamingEvents()
  initStreamingEvents()
}
