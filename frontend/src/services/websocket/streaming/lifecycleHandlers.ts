/**
 * 生命周期事件处理器（STATE_CHANGE / PIPELINE_RECEIVED / WS重连补漏 / chunk超时回调）
 *
 * 从 initStreamingEvents 中提取的独立处理器函数，降低 index.ts 复杂度。
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'
import { generateUUID } from '@/utils/uuid'

import { allocateNextSequence, terminatePipeline } from './handlers/utils'
import { resolvePipelineId } from './router'

/** 重连后补漏轮询间隔（5秒） */
const RECONNECT_POLL_INTERVAL_MS = 5_000
/** 重连后补漏最大轮询时长（3分钟） */
const RECONNECT_POLL_MAX_DURATION_MS = 180_000

/**
 * 处理 STATE_CHANGE 事件
 */
export function handleStateChange(eventData: any): void {
  const status = eventData?.data?.status || eventData?.status
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData?.data?.thread_id || eventData?.thread_id

  if (status === 'suspended' && pipelineId) {
    terminatePipeline(pipelineId, threadId)
    loggers.sessionStore.info('[STATE_CHANGE] pipeline suspended → streaming cleaned: pipeline=%s', pipelineId)
  }
}

/**
 * 处理 PIPELINE_RECEIVED 事件（非用户消息注入）
 *
 * 当后端通过管道注入非用户来源的消息（如任务触发、定时器等）时，
 * 创建一个 system 角色的消息气泡显示在聊天界面中。
 * 若 source === 'user' 则跳过（前端已在 handleSendMessage 中创建了用户消息气泡）。
 * 包含基于 content 前缀的去重逻辑，防止重复创建相同内容的系统消息。
 */
export function handlePipelineReceived(data: any): void {
  const pipelineId = resolvePipelineId(data)
  if (!pipelineId) return

  const payload = data?.data || data
  const content = payload?.content || ''
  const source = payload?.source || ''

  // 用户消息已由 handleSendMessage 处理，此处跳过
  if (source === 'user') return

  // 无内容则无需创建气泡
  if (!content) return

  const pipelineStore = usePipelineMessageStore.getState()

  // 去重：检查管道中是否已存在相同 content 前缀的 system 消息
  const existingMsgs = pipelineStore.getMessages(pipelineId)
  const dedupPrefix = content.substring(0, 60)
  const alreadyExists = existingMsgs.some((m: any) => {
    if (m.role === 'system') {
      const mContent = m.content || ''
      if (mContent && mContent.includes(dedupPrefix)) return true
      const parts = m.parts || []
      if (parts.some((p: any) => p.type === 'system' && (p.content || '').includes(dedupPrefix))) return true
    }
    return false
  })

  if (alreadyExists) {
    loggers.sessionStore.info(
      '[handlePipelineReceived] dedup: skipping duplicate inject: pipeline=%s prefix=%.30s',
      pipelineId.slice(0, 12), dedupPrefix,
    )
    return
  }

  // BUG-FIX-fix_20260529_msg_order: 优先使用后端返回的真实 sequence
  // 问题根因: 前端自算 sequence 与后端不一致
  // 修复方案: 从 WS 事件中提取后端 sequence，优先使用；fallback 到自算
  const backendSeq = data?.sequence || payload?.sequence
  const placeholderSeq = allocateNextSequence(pipelineId, backendSeq)

  pipelineStore.addMessage(pipelineId, {
    id: `sys_${generateUUID()}`,
    role: 'system',
    content,
    sequence: placeholderSeq,
    timestamp: new Date().toISOString(),
    status: 'completed',
    parts: [
      {
        type: 'system',
        content,
        level: 'info',
        notificationType: 'pipeline_inject',
        sequence: 0,
      },
    ],
    metadata: {
      record_type: 'system',
      type: 'system',
      sender_type: 'system',
      source,
    },
  } as any)
}

/**
 * 重连后补漏轮询：fetchMessages 失败或返回空时，启动定时轮询
 * 直到 streaming 状态结束或超时（最多 3 分钟）
 */
function _startReconnectPolling(
  pipelineId: string,
  sessionId: string,
  initialCursor: number,
  logger: ReturnType<typeof loggers.sessionStore>,
): void {
  const startTime = Date.now()

  const pollTimer = setInterval(() => {
    const pipelineStore = usePipelineMessageStore.getState()

    // streaming 已结束，停止轮询
    if (!pipelineStore.isStreaming(pipelineId)) {
      clearInterval(pollTimer)
      return
    }

    // 超过最大轮询时长，停止轮询
    if (Date.now() - startTime > RECONNECT_POLL_MAX_DURATION_MS) {
      logger.warn('[streaming] 重连补漏轮询超时: pipelineId=%s', pipelineId)
      clearInterval(pollTimer)
      return
    }

    const bottomCursor = pipelineStore.getBottomCursor(pipelineId)
    pipelineStore.fetchMessages(pipelineId, {
      after_sequence: bottomCursor,
      threadId: sessionId,
    }).catch((err) => {
      logger.warn('[streaming] 重连补漏轮询失败: pipelineId=%s err=%s', pipelineId, err)
    })
  }, RECONNECT_POLL_INTERVAL_MS)
}

/**
 * 处理 WS 重连补漏
 */
export function handleReconnected(): void {
  const pipelineStore = usePipelineMessageStore.getState()
  const streamingState = pipelineStore.streamingState
  const streamingStore = useStreamingStore.getState()
  const logger = loggers.sessionStore

  logger.info('[streaming] WS 重连，开始补偿遗漏消息，streaming 管道数=%d', Object.keys(streamingState).length)

  // 遍历所有管道消息，将 parts 中 state='streaming' 的 thinking part 强制清理为 done
  const messagesByPipeline = pipelineStore.messagesByPipeline
  for (const [pipelineId, messages] of Object.entries(messagesByPipeline)) {
    const stuckMessages = (messages as any[]).filter(
      (m: any) => (m.parts || []).some((p: any) => p.type === 'thinking' && p.state === 'streaming'),
    )
    for (const msg of stuckMessages) {
      logger.info('[streaming] 重连清理残留 streaming thinking part: pipelineId=%s messageId=%s', pipelineId, msg.id)
      // 将所有 streaming 状态的 parts 改为 done
      const updatedParts = (msg.parts as any[]).map((p: any) =>
        p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
      )
      pipelineStore.updateMessage(pipelineId, msg.id, { parts: updatedParts } as any)
    }
  }

  // 清理 streamingStore 中残留的 thinking 状态
  const streamingTabs = streamingStore.streamingTabs
  for (const tabId of Object.keys(streamingTabs)) {
    if (!streamingState[tabId]?.isStreaming) {
      streamingStore.setStreamingForTab(tabId, false)
    }
  }

  for (const [pipelineId, streamStatus] of Object.entries(streamingState)) {
    if (!streamStatus.isStreaming) continue

    const bottomCursor = pipelineStore.getBottomCursor(pipelineId)
    const sessionId = pipelineStore.pipelineSessionMap[pipelineId]

    if (sessionId) {
      pipelineStore.fetchMessages(pipelineId, {
        after_sequence: bottomCursor,
        threadId: sessionId,
      }).then(() => {
        const currentStore = usePipelineMessageStore.getState()
        if (currentStore.isStreaming(pipelineId)) {
          _startReconnectPolling(pipelineId, sessionId, bottomCursor, logger)
        }
      }).catch((err) => {
        logger.warn('[streaming] 重连补漏失败，启动轮询: pipelineId=%s err=%s', pipelineId, err)
        _startReconnectPolling(pipelineId, sessionId, bottomCursor, logger)
      })
    } else {
      // BUG-FIX-fix_20260523_reconnect_missing_session:
      // 问题根因: pipelineSessionMap 中没有对应 pipelineId 的 sessionId 时，
      //          该管道被完全跳过，无任何 fallback，streaming 状态残留。
      // 修复方案: 尝试从 agentTabStore 的 pipelineTabMap 获取关联信息作为 fallback；
      //          如果仍然没有，打印 warn 并清理该管道的 streaming 状态。
      // 影响范围: 重连后的消息补漏完整性
      // 修复日期: 2026-05-23
      const agentTabStore = useAgentTabStore.getState()
      const fallbackTabId = agentTabStore.pipelineTabMap[pipelineId]
      const fallbackTab = fallbackTabId
        ? agentTabStore.tabs.find((t) => t.id === fallbackTabId)
        : null
      const fallbackSessionId = fallbackTab?.parentRecordId || agentTabStore.currentSessionId || ''

      if (fallbackSessionId) {
        logger.warn('[streaming] 重连补漏 pipelineSessionMap 缺失，使用 fallback sessionId: pipelineId=%s fallbackSessionId=%s', pipelineId, fallbackSessionId)
        pipelineStore.fetchMessages(pipelineId, {
          after_sequence: bottomCursor,
          threadId: fallbackSessionId,
        }).then(() => {
          const currentStore = usePipelineMessageStore.getState()
          if (currentStore.isStreaming(pipelineId)) {
            _startReconnectPolling(pipelineId, fallbackSessionId, bottomCursor, logger)
          }
        }).catch((err) => {
          logger.warn('[streaming] 重连补漏 fallback 失败: pipelineId=%s err=%s', pipelineId, err)
        })
      } else {
        logger.warn('[streaming] 重连补漏无可用 sessionId，清理 streaming 状态: pipelineId=%s', pipelineId)
        pipelineStore.stopStreaming(pipelineId)
        streamingStore.setStreamingForTab(pipelineId, false)
      }
    }
  }
}

/**
 * 处理 chunk 超时回调
 */
export function handleChunkTimeout(data: { pipelineId: string; messageId: string }): void {
  const { pipelineId, messageId } = data
  const pipelineStore = usePipelineMessageStore.getState()
  const streamingStore = useStreamingStore.getState()
  const logger = loggers.sessionStore

  logger.warn('[streaming] chunk 超时，清理管道状态（保留已累积内容）: pipelineId=%s messageId=%s', pipelineId, messageId)

  // BUG-FIX-fix_20260523_streaming_timeout_blank:
  // 问题根因: chunk 超时时无条件将消息标记为 completed，但未检查消息是否有实际内容。
  //          当后端 LLM 响应慢或完全失败时，消息内容为空，用户看到空白消息且无任何错误提示。
  // 修复方案: 超时时先检查消息是否有内容，有内容则保留并标记 completed，无内容则标记 error 并通知用户。
  //
  // BUG-FIX-fix_20260523_unified_timeout_silent_drop:
  // 问题根因: 统一流式超时（120s）传入 messageId=''，导致 if (messageId) 为 false，
  //          跳过所有超时处理，消息被静默丢弃，用户无任何反馈。
  // 修复方案: 即使 messageId 为空，也要将 streaming 状态的消息标记为 error 并通知用户。
  // 影响范围: 统一流式超时后的用户体验
  // 修复日期: 2026-05-23
  if (messageId) {
    const msgs = pipelineStore.getMessages(pipelineId)
    const msg = msgs.find((m: any) => m.id === messageId)
    if (msg) {
      // 基于 parts[] 检查消息是否有实际内容
      const hasContent = !!(msg as any).content?.trim()
        || (msg.parts || []).some((p: any) => p.type === 'text' && p.text?.trim())

      // 将所有 streaming 状态的 parts 改为 done
      const finalizeParts = (m: any): any[] =>
        (m.parts || []).map((p: any) =>
          p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
        )

      if (hasContent) {
        pipelineStore.updateMessage(pipelineId, messageId, {
          status: 'completed',
          parts: finalizeParts(msg),
        } as any)
      } else {
        pipelineStore.updateMessage(pipelineId, messageId, {
          status: 'error',
          parts: finalizeParts(msg),
        } as any)
        useNotificationStore.getState().addNotification({
          title: '响应超时',
          message: '响应超时，请重试',
          priority: 'high',
          category: 'error',
          isBlocking: false,
        })
      }
    }
  } else {
    // 统一流式超时（messageId 为空）：查找该管道中 streaming 状态的消息并标记 error
    const msgs = pipelineStore.getMessages(pipelineId)
    const streamingMsg = msgs.find((m: any) => m.status === 'streaming' || m.status === 'pending')
    if (streamingMsg) {
      // 将所有 streaming 状态的 parts 改为 done
      const finalizeParts = (m: any): any[] =>
        (m.parts || []).map((p: any) =>
          p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
        )
      pipelineStore.updateMessage(pipelineId, (streamingMsg as any).id, {
        status: 'error',
        parts: finalizeParts(streamingMsg),
      } as any)
    }
    useNotificationStore.getState().addNotification({
      title: '响应超时',
      message: '等待响应超时，请重试',
      priority: 'high',
      category: 'error',
      isBlocking: false,
    })
  }

  // 清理 streaming 状态
  if (pipelineStore.isStreaming(pipelineId)) {
    pipelineStore.stopStreaming(pipelineId)
  }
  streamingStore.setStreamingForTab(pipelineId, false)
}

/**
 * 处理 SYSTEM_NOTIFICATION 事件（任务完成/失败等系统通知）
 *
 * 通过统一流式路径（bridge on_chunk → drain_loop → WebSocket）发送，
 * 将系统通知作为独立消息添加到管道消息列表中渲染。
 *
 * BUG-FIX-fix_20260524_duplicate_notification:
 * 问题根因: task_notifier 同时通过两条路径发送同一通知：
 *          1) send_pipeline_message → 注入为 role:'user' 消息（后端管道记录，前端已过滤）
 *          2) send_frontend_event → system_notification WS事件（实时显示为系统气泡）
 *          导致用户看到两条一模一样的通知，气泡样式不同（用户气泡 vs 系统气泡）。
 * 修复方案: ChatContainer 过滤后端注入的 [系统通知] user 消息；
 *          此处仅保留 system 消息自身去重（防止 WS 事件重复发送）。
 * 影响范围: 任务完成/失败通知的显示
 * 修复日期: 2026-05-24
 */
export function handleSystemNotification(eventData: any): void {
  const pipelineId = resolvePipelineId(eventData)
  const data = eventData?.data || eventData
  const content = data?.content || ''
  const level = data?.level || 'info'
  const notificationType = data?.notificationType || ''

  if (!pipelineId || !content) return

  const pipelineStore = usePipelineMessageStore.getState()

  const existingMsgs = pipelineStore.getMessages(pipelineId)
  // BUG-FIX-fix_20260529_msg_order: 优先使用后端返回的真实 sequence
  // 问题根因: 前端自算 sequence 与后端不一致
  // 修复方案: 从 WS 事件中提取后端 sequence，优先使用；fallback 到自算
  const backendSeq = data?.sequence || eventData?.data?.sequence
  const nextSeq = allocateNextSequence(pipelineId, backendSeq)
  const dedupPrefix = content.substring(0, 60)
  const alreadyExists = existingMsgs.some((m: any) => {
    if (m.role === 'system') {
      const mContent = m.content || ''
      if (mContent && mContent.includes(dedupPrefix)) return true
      const parts = m.parts || []
      if (parts.some((p: any) => p.type === 'system' && (p.content || '').includes(dedupPrefix))) return true
    }
    return false
  })

  if (alreadyExists) {
    loggers.sessionStore.info(
      '[handleSystemNotification] dedup: skipping duplicate notification: pipeline=%s prefix=%.30s',
      pipelineId?.slice(0, 12), dedupPrefix,
    )
    return
  }

  pipelineStore.addMessage(pipelineId, {
    id: `sys_${generateUUID()}`,
    role: 'system',
    content,
    sequence: nextSeq,
    timestamp: new Date().toISOString(),
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
    },
  } as any)
}
