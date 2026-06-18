/**
 * 生命周期事件处理器（STATE_CHANGE / WS重连补漏 / 系统通知）
 *
 * 从 initStreamingEvents 中提取的独立处理器函数，降低 index.ts 复杂度。
 */
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'
import { generateUUID } from '@/utils/uuid'

import { allocateNextSequence, terminatePipeline } from './handlers/utils'
import { resolvePipelineId } from './router'

/**
 * 处理 STATE_CHANGE 事件
 */
export function handleStateChange(eventData: any): void {
  const status = eventData?.data?.status || eventData?.status
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData?.data?.thread_id || eventData?.thread_id

  // BUG-FIX-fix_20260617_state_change_whitelist:
  // 问题根因: 原代码只处理 status === 'suspended'，遗漏 stopped/finished/failed/completed，
  //   导致用户点"停止生成"或管道异常终止时，前端 streamingState 永远不被清理，UI 永久转圈。
  // 修复方案: 扩展终态白名单，所有终态都调用 terminatePipeline 清理 streamingState。
  // 影响范围: 停止按钮、异常终止、正常完成等场景的 streaming 清理
  // 修复日期: 2026-06-17
  const TERMINAL_STATUSES = ['suspended', 'stopped', 'finished', 'failed', 'completed', 'cancelled']
  if (pipelineId && TERMINAL_STATUSES.includes(status)) {
    terminatePipeline(pipelineId, threadId)
    loggers.sessionStore.info('[STATE_CHANGE] pipeline %s → streaming cleaned: pipeline=%s', status, pipelineId)
  }
}

/**
 * 处理 WS 重连补漏
 *
 * 后端 session_manager 通过 missed_messages 事件主动推送补偿光标。
 * 前端只做必要的清理（stuck streaming parts）和基于 streamingState 的 fetch。
 */
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
  for (const pipelineId of streamingPipelineIds) {
    logger.info('[streaming] 跳过流式管道 %s 的补漏 fetch（避免竞态）', pipelineId.slice(0, 12))
  }

  // BUG-FIX-fix_20260617_streaming_gap_no_notify:
  // 问题根因: WS 重连后跳过流式管道补漏 fetch，断连期间流式消息永久丢失，
  //          且用户无任何感知。
  // 修复方案: 补漏 fetch 存在竞态风险无法安全实现，至少通知用户流式消息可能丢失，
  //          引导用户手动检查或刷新。
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

/**
 * 处理 SYSTEM_NOTIFICATION 事件（任务完成/失败等系统通知）
 *
 * 系统消息气泡的唯一创建入口。后端通过 send_frontend_event 发送
 * system_notification WS 事件，此处接收并添加到管道消息列表。
 *
 * 去重策略：精确内容匹配（不使用 includes，避免相似内容被误判为重复）。
 */
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
      // BUG-FIX-M03: WS handler 层 console 残留
      // 问题根因: 降级去重路径用 console.warn 记录。
      // 修复方案: 改用正式 logger.warn。
      loggers.websocket.warn('[系统通知] notification_id 缺失，使用内容去重: %.40s', content.slice(0, 40))
      return
    }
  }

  loggers.websocket.debug(
    '[MSG-LIFE] 系统通知创建: pipeline=%s content=%.40s',
    pipelineId.slice(0, 12), content.slice(0, 40),
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
