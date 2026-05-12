/**
 * 全局流式事件服务（管道 ID 路由版本）
 *
 * 核心设计原则：
 * - pipeline_id 是唯一的路由键
 * - 所有消息操作统一通过 pipelineMessageStore
 * - 每个管道独立渲染，互不干扰
 * - 主管道是默认管道（无 pipeline_id 时用 session_id 充当）
 * - 后台会话的管道也能独立流式输出
 */
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'

import { clearAllChunkTimeouts, destroyVisibilityListener, initVisibilityListener } from './chunkTimeout'
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
} from './handlers'

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}

/**
 * 初始化全局流式事件处理器（幂等，重复调用安全）
 */
export function initStreamingEvents(): void {
  if (_initialized) return
  _initialized = true

  _handlers[WS_SERVER_EVENTS.STREAM_START] = handleStreamStart
  _handlers[WS_SERVER_EVENTS.STREAM_CHUNK] = handleStreamChunk
  _handlers[WS_SERVER_EVENTS.STREAM_END] = handleStreamEnd
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = handleStreamError
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = handleNewMessage
  _handlers[WS_SERVER_EVENTS.THINKING_START] = handleThinkingStart
  _handlers[WS_SERVER_EVENTS.THINKING_CHUNK] = handleThinkingChunk
  _handlers[WS_SERVER_EVENTS.THINKING_END] = handleThinkingEnd
  _handlers[WS_SERVER_EVENTS.TOOL_START] = handleToolStart
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = handleToolResult
  _handlers[WS_SERVER_EVENTS.SUB_AGENT_CREATED] = handleSubAgentCreated
  _handlers[WS_SERVER_EVENTS.STREAM_KEEPALIVE] = handleStreamKeepalive

  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.subscribe(event, handler)
  }

  initVisibilityListener()
}

/**
 * 销毁全局流式事件处理器
 */
export function destroyStreamingEvents(): void {
  if (!_initialized) return
  clearAllChunkTimeouts()
  destroyVisibilityListener()
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
