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
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

import { clearAllChunkTimeouts, clearChunkTimeout, getChunkTimeoutMessageId, onChunkTimeout, resetChunkTimeout } from './chunkTimeout'
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
 * 初始化全局流式事件处理器（幂等，重复调用安全）
 */
export function initStreamingEvents(): void {
  if (_initialized) return
  _initialized = true

  _handlers[WS_SERVER_EVENTS.STREAM_START] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.STREAM_START, handleStreamStart)
  _handlers[WS_SERVER_EVENTS.STREAM_CHUNK] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk)
  _handlers[WS_SERVER_EVENTS.STREAM_END] = handleStreamEnd
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = handleStreamError
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = handleNewMessage
  _handlers[WS_SERVER_EVENTS.THINKING_START] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.THINKING_START, handleThinkingStart)
  _handlers[WS_SERVER_EVENTS.THINKING_CHUNK] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.THINKING_CHUNK, handleThinkingChunk)
  _handlers[WS_SERVER_EVENTS.THINKING_END] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.THINKING_END, handleThinkingEnd)
  _handlers[WS_SERVER_EVENTS.TOOL_START] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.TOOL_START, handleToolStart)
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult)
  _handlers[WS_SERVER_EVENTS.SUB_AGENT_CREATED] = handleSubAgentCreated
  _handlers[WS_SERVER_EVENTS.STREAM_KEEPALIVE] = _wrapWithTimeoutReset(WS_SERVER_EVENTS.STREAM_KEEPALIVE, handleStreamKeepalive)

  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.subscribe(event, handler)
  }

  // BUG-FIX-fix_20260513_reconnect_msg_compensation:
  // 问题根因: WebSocket 断线重连后，断线期间的消息不会自动补偿，
  //          导致 streaming 消息永远停留在 streaming 状态。
  // 修复方案: 监听 GlobalWebSocket 的 'reconnected' 事件，对正在 streaming 的管道
  //          调用 fetchMessages(after_sequence: bottomCursor) 做断线补漏，
  //          同时清理超时的 streaming 状态。
  // 影响范围: WebSocket 断线重连后的消息完整性
  // 修复日期: 2026-05-13
  _handlers['reconnected'] = () => {
    const pipelineStore = usePipelineMessageStore.getState()
    const streamingState = pipelineStore.streamingState
    const logger = loggers.sessionStore

    logger.info('[streaming] WS 重连，开始补偿遗漏消息，streaming 管道数=%d', Object.keys(streamingState).length)

    for (const [pipelineId, streamStatus] of Object.entries(streamingState)) {
      if (!streamStatus.isStreaming) continue

      const bottomCursor = pipelineStore.getBottomCursor(pipelineId)
      const sessionId = pipelineStore.pipelineSessionMap[pipelineId]

      if (sessionId) {
        pipelineStore.fetchMessages(pipelineId, {
          after_sequence: bottomCursor,
          threadId: sessionId,
        }).catch((err) => {
          logger.warn('[streaming] 重连补漏失败: pipelineId=%s err=%s', pipelineId, err)
        })
      }
    }
  }
  globalWS.subscribe('reconnected', _handlers['reconnected'])

  // BUG-FIX-fix_20260513_chunk_timeout_cleanup:
  // 问题根因: chunkTimeout 超时后只打 console.debug，不清理 streaming 状态、
  //          不标记消息为 error、不通知用户，导致消息永远卡在 streaming 状态。
  // 修复方案: 通过 onChunkTimeout 回调注册，在超时时将对应消息标记为 error 状态并清理 streaming。
  // 影响范围: 流式响应超时后的用户体验
  // 修复日期: 2026-05-13
  onChunkTimeout((data: { pipelineId: string; messageId: string }) => {
    const { pipelineId, messageId } = data
    const pipelineStore = usePipelineMessageStore.getState()
    const streamingStore = useStreamingStore.getState()
    const logger = loggers.sessionStore

    logger.warn('[streaming] chunk 超时，清理管道状态: pipelineId=%s messageId=%s', pipelineId, messageId)

    // 有 messageId 时将消息标记为 error 状态
    if (messageId) {
      pipelineStore.updateMessage(pipelineId, messageId, {
        status: 'error',
        error: '流式响应超时，请重试',
      } as any)
    }

    // 清理 streaming 状态
    if (pipelineStore.isStreaming(pipelineId)) {
      pipelineStore.stopStreaming(pipelineId)
    }
    streamingStore.setStreamingForTab(pipelineId, false)
  })
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
