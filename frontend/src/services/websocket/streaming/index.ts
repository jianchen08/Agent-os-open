/** 全局流式事件服务（管道 ID 路由版本） 核心设计原则： */
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { loggers } from '@/utils/logger'

import {
  handleBlockEnd,
  handleBlockStart,
  handleFinish,
  handleNewMessage,
  handlePluginError,
  handleReasoningDelta,
  handleStreamEnd,
  handleStreamError,
  handleStreamStart,
  handleTextDelta,
  handleToolCallDelta,
  handleToolProgress,
  handleToolResult,
  handleToolStart,
  handleUsage,
  handleIteration,
} from './handlers'
import {
  handleCostUpdate,
  handleReconnected,
  handleSystemNotification,
  handleTerminationStatus,
} from './lifecycleHandlers'
import { isPipelineRelevant, resolvePipelineId } from './router'

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}
const _debugLogger = loggers.websocket

/** 全局 WS 事件日志包装器 记录每一个到达前端的 WS 事件类型、pipelineId、messageId， */
function _logEvent(eventType: string, data: any): void {
  // 高频增量事件跳过日志（防流式刷屏）：text/reasoning/tool-call 增量与 keepalive
  if (
    eventType === 'text_delta' || eventType === 'reasoning_delta' ||
    eventType === 'tool_call_delta' || eventType === 'keepalive'
  ) return
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
    // 中央门控：非关注 pipeline 的流式事件直接丢弃，不注册幽灵管道、不写 store。
    // 仅当事件携带 pipelineId 时才过滤；会话级/全局事件（pid 为空）照常放行。
    const pid = resolvePipelineId(data)
    if (pid && !isPipelineRelevant(pid)) {
      _debugLogger.info(`[STREAM] drop irrelevant pipeline event: ${event} pid=${pid.slice(0, 12)}`)
      return
    }
    handler(data)
  }

  _handlers[WS_SERVER_EVENTS.STREAM_START] = _logWrap(WS_SERVER_EVENTS.STREAM_START, handleStreamStart)
  _handlers[WS_SERVER_EVENTS.BLOCK_START] = _logWrap(WS_SERVER_EVENTS.BLOCK_START, handleBlockStart)
  _handlers[WS_SERVER_EVENTS.TEXT_DELTA] = _logWrap(WS_SERVER_EVENTS.TEXT_DELTA, handleTextDelta)
  _handlers[WS_SERVER_EVENTS.REASONING_DELTA] = _logWrap(WS_SERVER_EVENTS.REASONING_DELTA, handleReasoningDelta)
  _handlers[WS_SERVER_EVENTS.TOOL_CALL_DELTA] = _logWrap(WS_SERVER_EVENTS.TOOL_CALL_DELTA, handleToolCallDelta)
  _handlers[WS_SERVER_EVENTS.BLOCK_END] = _logWrap(WS_SERVER_EVENTS.BLOCK_END, handleBlockEnd)
  _handlers[WS_SERVER_EVENTS.USAGE_EVENT] = _logWrap(WS_SERVER_EVENTS.USAGE_EVENT, handleUsage)
  _handlers[WS_SERVER_EVENTS.FINISH] = _logWrap(WS_SERVER_EVENTS.FINISH, handleFinish)
  _handlers[WS_SERVER_EVENTS.STREAM_END] = _logWrap(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd)
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = _logWrap(WS_SERVER_EVENTS.STREAM_ERROR, handleStreamError)
  _handlers[WS_SERVER_EVENTS.PLUGIN_ERROR] = _logWrap(WS_SERVER_EVENTS.PLUGIN_ERROR, handlePluginError)
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = _logWrap(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)
  _handlers[WS_SERVER_EVENTS.TOOL_START] = _logWrap(WS_SERVER_EVENTS.TOOL_START, handleToolStart)
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = _logWrap(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult)
  _handlers[WS_SERVER_EVENTS.TOOL_PROGRESS] = _logWrap(WS_SERVER_EVENTS.TOOL_PROGRESS, handleToolProgress)
  _handlers[WS_SERVER_EVENTS.ITERATION] = _logWrap(WS_SERVER_EVENTS.ITERATION, handleIteration)
  // keepalive（LLM 流式 8 事件协议：超时探活）沿用心跳语义——无业务载荷，
  // 不订阅不处理：GlobalWebSocket 连接级心跳负责保活，流式 keepalive 仅表明
  // 上游存活，前端无消费面（事件名透传到达时无订阅即静默）。

  _handlers[WS_SERVER_EVENTS.COST_UPDATE] = _logWrap(WS_SERVER_EVENTS.COST_UPDATE, handleCostUpdate)
  _handlers[WS_SERVER_EVENTS.TERMINATION_STATUS] = _logWrap(WS_SERVER_EVENTS.TERMINATION_STATUS, handleTerminationStatus)
  // 2026-08-26 接线：后端 chat.send_message 后台派发失败经此补报
  // （统一错误模型：假成功显式化），前端渲染 system 错误气泡。
  _handlers[WS_SERVER_EVENTS.SYSTEM_NOTIFICATION] = _logWrap(
    WS_SERVER_EVENTS.SYSTEM_NOTIFICATION,
    handleSystemNotification,
  )

  // 2026-08-26 清理：以下事件在后端（kernel ws_session.rs / capability_router.rs
  // 事件族 + 插件 event-bus.emit 全集）无任何发射源，订阅已删除：
  // error / sub_agent_created / state_change / stream_chunk / thinking_start /
  // thinking_chunk / thinking_end / stream_keepalive。
  // 流式正文/思考已迁移到 8 事件协议（block_start/text_delta/reasoning_delta/
  // tool_call_delta/block_end/usage/finish/keepalive，见 blockHandler.ts）。

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
