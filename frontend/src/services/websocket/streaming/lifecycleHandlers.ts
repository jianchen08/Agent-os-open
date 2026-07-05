/** 生命周期事件处理器（STATE_CHANGE / WS重连补漏 / 系统通知 / 用量更新） 从 initStreamingEvents 中提取的独立处理器函数，降低 index.ts 复杂度。 */
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'
import { generateUUID } from '@/utils/uuid'

import { allocateNextSequence, terminatePipeline } from './handlers/utils'
import { resolvePipelineId } from './router'

/** 处理 STATE_CHANGE 事件 */
export function handleStateChange(eventData: any): void {
  const status = eventData?.data?.status || eventData?.status
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData?.data?.thread_id || eventData?.thread_id

  const TERMINAL_STATUSES = ['suspended', 'stopped', 'finished', 'failed', 'completed', 'cancelled']
  if (pipelineId && TERMINAL_STATUSES.includes(status)) {
    terminatePipeline(pipelineId, threadId)
    loggers.sessionStore.info('[STATE_CHANGE] pipeline %s → streaming cleaned: pipeline=%s', status, pipelineId)
  }
}

/** 处理 WS 重连补漏 后端 session_manager 通过 missed_messages 事件主动推送补偿光标。 */
export function handleReconnected(): void {
  const pipelineStore = usePipelineMessageStore.getState()
  const streamingState = pipelineStore.streamingState
  const logger = loggers.sessionStore

  logger.info('[streaming] WS 重连，清理残留状态，streaming 管道数=%d', Object.keys(streamingState).length)

  // 清理残留 streaming thinking parts
  const messagesByPipeline = pipelineStore.messagesByPipeline
  for (const [pipelineId, messages] of Object.entries(messagesByPipeline)) {
    const stuckMessages = (messages as any[]).filter(
      (m: any) => (m.parts || []).some((p: any) => p.type === 'thinking' && p.state === 'streaming'),
    )
    for (const msg of stuckMessages) {
      const updatedParts = (msg.parts as any[]).map((p: any) =>
        p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
      )
      pipelineStore.updateMessage(pipelineId, msg.id, { parts: updatedParts } as any)
    }
  }

  // 为 streaming 管道补漏
  const streamingPipelineIds = Object.keys(streamingState).filter(
    (pipelineId) => streamingState[pipelineId]?.isStreaming,
  )
  // streamingState 中已有旧记录，占位创建/更新失败，AI 回复无法显示。
  for (const pipelineId of streamingPipelineIds) {
    // 将残留的 streaming 占位消息标记为 completed，避免 UI 永久转圈
    const messages = pipelineStore.messagesByPipeline[pipelineId] || []
    for (const msg of messages as any[]) {
      if (msg.role === 'assistant' && msg.status === 'streaming') {
        pipelineStore.updateMessage(pipelineId, msg.id, { status: 'completed' } as any)
      }
    }
    // 清理 streamingState（同时处理 threadId）
    const threadId = pipelineStore.pipelineSessionMap[pipelineId]
    terminatePipeline(pipelineId, threadId !== pipelineId ? threadId : undefined)
    logger.info('[streaming] 终止残留流式管道 %s，清理 streamingState', pipelineId.slice(0, 12))
  }

  if (streamingPipelineIds.length > 0) {
    useNotificationStore.getState().addNotification({
      title: '流式消息可能丢失',
      message: `WebSocket 重连期间有 ${streamingPipelineIds.length} 个流式管道可能丢失消息，请检查相关会话或手动刷新`,
      priority: 'high',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 10000,
    })
  }
}

/** 处理 SYSTEM_NOTIFICATION 事件（任务完成/失败等系统通知） 系统消息气泡的唯一创建入口。后端通过 send_frontend_event 发送 */
export function handleSystemNotification(eventData: any): void {
  const pipelineId = resolvePipelineId(eventData)
  const data = eventData?.data || eventData
  const content = data?.content || ''
  const level = data?.level || 'info'
  const notificationType = data?.notificationType || ''
  const notificationId = data?.notification_id || ''

  if (!pipelineId || !content) return

  const pipelineStore = usePipelineMessageStore.getState()

  const existingMsgs = pipelineStore.getMessages(pipelineId)
  // notification_id 为空时走内容精确去重，避免空字符串匹配所有通知
  if (notificationId) {
    const alreadyExists = existingMsgs.some((m: any) => {
      if (m.role !== 'system') return false
      const metaId = (m as any).metadata?.notification_id || ''
      return metaId === notificationId
    })
    if (alreadyExists) return
  } else {
    // notification_id 缺失时，用 content 精确匹配去重
    const alreadyExists = existingMsgs.some((m: any) => {
      if (m.role !== 'system') return false
      return m.content === content
    })
    if (alreadyExists) {
      // -M03: WS handler 层 console 残留
      loggers.websocket.warn('[系统通知] notification_id 缺失，使用内容去重: %.40s', content.slice(0, 40))
      return
    }
  }

  loggers.websocket.debug(
    '[MSG-LIFE] 系统通知创建: pipeline=%s content=%.40s',
    pipelineId.slice(0, 12), content.slice(0, 40),
  )

  // ★ 诊断：notification 到达时的 store 状态（INFO 级别确保可见）
  const _diagBefore = pipelineStore.getMessages(pipelineId)
  const _diagLast = _diagBefore[_diagBefore.length - 1]
  loggers.websocket.info(
    '[NOTIF-ARRIVE] total=%d last=[%s/%s/%s] seq=%s',
    _diagBefore.length,
    _diagLast?.role ?? 'null',
    _diagLast?.status ?? 'null',
    (_diagLast?.id ?? '').slice(0, 10),
    data?.sequence ?? 'none',
  )

  pipelineStore.addMessage(pipelineId, {
    id: `sys_${generateUUID()}`,
    role: 'system',
    content,
    timestamp: new Date().toISOString(),
    sequence: allocateNextSequence(pipelineId, data?.sequence),
    parts: [
      {
        type: 'system',
        content,
        level: level as any,
        notificationType,
        sequence: 0,
      },
    ],
    status: 'completed',
    metadata: {
      record_type: 'system',
      type: 'system',
      sender_type: 'system',
      notification_level: level,
      notification_type: notificationType,
      notification_id: notificationId,  // Stage1: 保存notification_id用于去重
    },
  } as any)
}

/**
 * 处理 COST_UPDATE 事件：写入本轮单轮 token 用量到 contextUsageStore。
 *
 * 后端 track 插件在每轮 llm_call 后推送（tool_execute 轮已跳过），
 * payload = { pipeline_id, total_tokens, input_tokens, output_tokens }，
 * 均为本轮 API 返回的单轮值。进度条据此按 pipeline 实时刷新。
 */
export function handleCostUpdate(eventData: any): void {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const data = eventData?.data || eventData
  const totalTokens = data?.total_tokens || 0
  // 后端 tool_execute 轮已过滤，前端再兜底防 0 值覆盖
  if (totalTokens <= 0) return
  useContextUsageStore.getState().updateUsage(pipelineId, {
    total_tokens: totalTokens,
    input_tokens: data?.input_tokens || 0,
    output_tokens: data?.output_tokens || 0,
  })
}
