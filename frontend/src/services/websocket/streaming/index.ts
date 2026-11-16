/** 全局流式事件服务（管道 ID 路由版本） 核心设计原则： */
import { WS_LOCAL_EVENTS, WS_SERVER_EVENTS } from '@/constants/websocket'
import { autoOpenBrowserLiveOnToolStart } from '@/services/browserLiveAutoOpen'
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
  handlePipelineRoundFinished,
  handleRunFailed,
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
} from './lifecycleHandlers'
import { handleMemPressureNotice } from './memPressureNotice'
import { handlePluginUnavailableNotice } from './pluginHealthNotice'
import { isPipelineRelevant, resolvePipelineId } from './router'

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}
/**
 * 通知面直订 handler（独立于 _handlers 表，destroy 需单独反注册）：mem 族
 * （ADR 2026-10-19 D5 告警，record_id 与插件不可用同前缀但语义不同）先路由
 * 专项呈现，消费命中即短路；其余落插件健康面（卡3）。
 */
let _noticeRouteHandler: ((data: unknown) => void) | null = null
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
  // run 级收尾：整次执行结束（轮间 stream_end 不再终结生成态，终止信号在此）
  _handlers[WS_SERVER_EVENTS.PIPELINE_ROUND_FINISHED] = _logWrap(
    WS_SERVER_EVENTS.PIPELINE_ROUND_FINISHED,
    handlePipelineRoundFinished,
  )
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = _logWrap(WS_SERVER_EVENTS.STREAM_ERROR, handleStreamError)
  _handlers[WS_SERVER_EVENTS.PLUGIN_ERROR] = _logWrap(WS_SERVER_EVENTS.PLUGIN_ERROR, handlePluginError)
  // run 级失败终态镜像（ADR 2026-09-28-run-failure-frontend-notification）：
  // 绕过相关性门控直订——后台/未注册管道的失败正是要通知的场景，门控会丢弃
  // 未关注管道的事件；通知自携带路由坐标，注册表更新对未知管道 no-op。
  _handlers[WS_SERVER_EVENTS.RUN_FAILED] = (data) => {
    _logEvent(WS_SERVER_EVENTS.RUN_FAILED, data)
    handleRunFailed(data)
  }
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = _logWrap(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)
  _handlers[WS_SERVER_EVENTS.TOOL_START] = _logWrap(WS_SERVER_EVENTS.TOOL_START, (data) => {
    // 浏览器工具调用 → 自动打开/激活「浏览器」直播页签（幂等；用户裁定"使用即所见"）。
    // 门控后执行：非关注管道的浏览器调用不打扰当前视图。
    autoOpenBrowserLiveOnToolStart(data?.tool_name || data?.data?.tool_name)
    handleToolStart(data)
  })
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = _logWrap(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult)
  _handlers[WS_SERVER_EVENTS.TOOL_PROGRESS] = _logWrap(WS_SERVER_EVENTS.TOOL_PROGRESS, handleToolProgress)
  _handlers[WS_SERVER_EVENTS.ITERATION] = _logWrap(WS_SERVER_EVENTS.ITERATION, handleIteration)
  // keepalive（LLM 流式 8 事件协议：超时探活）沿用心跳语义——无业务载荷，
  // 不订阅不处理：GlobalWebSocket 连接级心跳负责保活，流式 keepalive 仅表明
  // 上游存活，前端无消费面（事件名透传到达时无订阅即静默）。

  _handlers[WS_SERVER_EVENTS.COST_UPDATE] = _logWrap(WS_SERVER_EVENTS.COST_UPDATE, handleCostUpdate)
  // 2026-08-26 接线：后端 chat.send_message 后台派发失败经此补报
  // （统一错误模型：假成功显式化），前端渲染 system 错误气泡。
  _handlers[WS_SERVER_EVENTS.SYSTEM_NOTIFICATION] = _logWrap(
    WS_SERVER_EVENTS.SYSTEM_NOTIFICATION,
    handleSystemNotification,
  )
  // 通知面直订：绕过相关性门控（RUN_FAILED 同款先例）——内存高压告警/插件
  // 降级横幅都是全局健康/治理面，非关注管道的事件也要点亮；门控内的气泡路径
  // （上方注册）维持原行为不变。
  _noticeRouteHandler = (data: unknown) => {
    _logEvent(WS_SERVER_EVENTS.SYSTEM_NOTIFICATION, data)
    if (handleMemPressureNotice(data)) return
    handlePluginUnavailableNotice(data)
  }
  globalWS.subscribe(WS_SERVER_EVENTS.SYSTEM_NOTIFICATION, _noticeRouteHandler)

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
  _handlers[WS_LOCAL_EVENTS.RECONNECTED] = handleReconnected
  globalWS.subscribe(WS_LOCAL_EVENTS.RECONNECTED, _handlers[WS_LOCAL_EVENTS.RECONNECTED])
}

/** 销毁全局流式事件处理器 */
export function destroyStreamingEvents(): void {
  if (!_initialized) return
  for (const [event, handler] of Object.entries(_handlers)) {
    globalWS.unsubscribe(event, handler)
  }
  if (_noticeRouteHandler) {
    globalWS.unsubscribe(WS_SERVER_EVENTS.SYSTEM_NOTIFICATION, _noticeRouteHandler)
    _noticeRouteHandler = null
  }
  Object.keys(_handlers).forEach((k) => delete _handlers[k])
  _initialized = false
}

/** 重新初始化流式事件处理器（销毁后重建） 用于以下场景： */
export function reinitStreamingEvents(): void {
  destroyStreamingEvents()
  initStreamingEvents()
}
