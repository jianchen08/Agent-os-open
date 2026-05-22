/**
 * 流式事件处理器（start / chunk / end / error / keepalive）
 *
 * 性能优化：stream_chunk 事件通过 RAF 批处理合并同一帧内的多个 chunk，
 * 将 N 次 Zustand 更新压缩为 1 次，显著降低流式输出期间的 UI 卡顿。
 */
import { reconcileContentBlocks } from '@/components/chat/hooks/useMessageRender'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

import { globalWS } from '@/services/websocket/GlobalWebSocket'

import { clearChunkTimeout, clearUnifiedStreamTimeout, getChunkTimeoutMessageId, resetChunkTimeout } from '../chunkTimeout'
import { appendTextBlock, appendThinkingChunk } from '../contentBlocks'
import { resolvePipelineId } from '../router'

import { ensureStreamingPlaceholder, extractMessageId, stopPipelineStreaming } from './utils'

const _debugLogger = loggers.websocket

// ── RAF 批处理：合并同一帧内的多个 stream_chunk 为单次 store 更新 ──

/** 每个 (pipelineId, messageId) 对应的待刷写 chunk 缓冲 */
const _chunkBuffer = new Map<string, {
  chunks: string[]
  pipelineId: string
  messageId: string
}>()
let _flushRafId: number | null = null

/** 将缓冲区的 chunk 合并后一次性写入 store */
function _flushChunks(): void {
  _flushRafId = null
  if (_chunkBuffer.size === 0) return

  const entries = [..._chunkBuffer.values()]
  _chunkBuffer.clear()

  for (const entry of entries) {
    const combinedContent = entry.chunks.join('')
    if (!combinedContent) continue

    const msgs = pipelineStore.getState().getMessages(entry.pipelineId)
    const msg = msgs.find((m: any) => m.id === entry.messageId)
    if (!msg) continue

    if ((msg as any).thinking?.isThinking) {
      const blocks = appendThinkingChunk(msg.contentBlocks, combinedContent)
      pipelineStore.getState().updateMessage(entry.pipelineId, entry.messageId, {
        thinking: { content: ((msg as any).thinking.content || '') + combinedContent, isThinking: true },
        contentBlocks: blocks,
      } as any)
    } else {
      const blocks = appendTextBlock(msg.contentBlocks, combinedContent, entry.messageId)
      pipelineStore.getState().updateMessage(entry.pipelineId, entry.messageId, {
        contentBlocks: blocks,
        content: (msg.content || '') + combinedContent,
      } as any)
    }
  }
}

/** 调度 RAF 刷写（幂等，同一帧内多次调用只触发一次） */
function _scheduleFlush(): void {
  if (_flushRafId === null) {
    _flushRafId = requestAnimationFrame(_flushChunks)
  }
}

/**
 * 立即刷写缓冲区。
 * streamEnd / streamError 必须在 reconcile 之前调用此方法，
 * 确保已缓冲的 chunk 全部写入 store 后再做最终合并。
 */
export function flushStreamChunkBuffer(): void {
  if (_flushRafId !== null) {
    cancelAnimationFrame(_flushRafId)
    _flushRafId = null
  }
  _flushChunks()
}

/**
 * 处理流式开始事件
 */
export function handleStreamStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // FIX: pipeline_id 为空时 warn 并 return，_threadId 不参与消息路由
    _debugLogger.warn(
      `[STREAM_START] pipeline_id missing, discarding event: _threadId=%s msgId=%s`,
      eventData._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
    )
    return
  }
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  const threadId = eventData.data?._threadId || eventData._threadId

  const currentActivePipelineId = pipelineStore.getState().activePipelineId
  _debugLogger.info(
    `[STREAM_START] pipelineId=${pipelineId.slice(0, 12)} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId.slice(0, 12)} activePipelineId=${currentActivePipelineId?.slice(0, 12) || 'null'}`,
  )

  // BUG-FIX-fix_20260521_stop_button_not_following_stream:
  // 问题根因: 当用户发送消息时，如果 activePipelineId 为空，会使用 sid 作为 fallback 管道。
  //          但后端处理消息后可能在另一个新创建的 pipeline 上触发流式输出。
  //          这导致 activePipelineId 与 stream_start 中的 pipelineId 不一致，
  //          effectiveIsGenerating 检测不到流式状态，停止按钮不显示。
  // 修复方案: 如果 activePipelineId 与 pipelineId 不一致，更新 activePipelineId。
  //          这样 effectiveIsGenerating 能正确检测到当前管道的流式状态。

  // BUG-FIX-fix_20260522_auto_switch_tab_on_submit:
  // 问题根因: 上述修复无条件切换 activePipelineId，当子 Agent 开始流式输出时，
  //          会将用户从主标签页强制跳转到子管道标签页，体验不佳。
  // 修复方案: 在调用 activatePipeline 之前增加条件判断，仅在以下场景激活管道：
  //          1. 当前没有活跃管道（首次初始化场景）
  //          2. 用户当前正在查看的 tab 对应的 pipelineRunId 与 pipelineId 匹配
  //          3. 用户已主动进入该管道的交互（interactionStore 中存在 entered 状态）
  //          4. 用户在主 tab 且该 pipelineId 不属于任何子 tab（主管道自身的流式输出）
  //          其他情况下不激活管道，让子管道的流式输出在后台进行。
  // 影响范围: 提交任务后标签页自动跳转行为
  // 修复日期: 2026-05-22
  if (currentActivePipelineId !== pipelineId) {
    const agentTabStore = useAgentTabStore.getState()
    const activeTab = agentTabStore.getActiveTab()
    const interactionStore = useInteractionStore.getState()

    /** 判断是否应该自动激活该管道 */
    const shouldActivatePipeline = (() => {
      // 条件1：当前没有活跃管道（首次初始化场景），允许激活
      if (!currentActivePipelineId) return true

      // 条件2：当前活跃 tab 的 pipelineRunId 等于 pipelineId，用户已在查看该管道
      if (activeTab?.pipelineRunId === pipelineId) return true

      // 条件3：当前活跃 tab 通过 pipelineTabMap 映射到该 pipelineId
      const tabIdForPipeline = agentTabStore.getTabIdByPipeline(pipelineId)
      if (tabIdForPipeline && tabIdForPipeline === agentTabStore.activeTabId) return true

      // 条件4：用户已主动进入该管道的交互（entered 状态）
      if (interactionStore.getEnteredForPipeline(pipelineId)) return true

      // 条件5：用户在主 tab 且该 pipelineId 不属于任何子 tab，
      //        认为是主管道自身的流式输出，应激活
      if (activeTab?.agentLevel === 1 && !tabIdForPipeline) return true

      return false
    })()

    if (shouldActivatePipeline) {
      _debugLogger.info(
        `[STREAM_START] activePipelineId changed: ${currentActivePipelineId?.slice(0, 12) || 'null'} -> ${pipelineId.slice(0, 12)}`,
      )
      pipelineStore.getState().activatePipeline(pipelineId)
    } else {
      _debugLogger.info(
        `[STREAM_START] skipping activatePipeline: user not viewing pipeline ${pipelineId.slice(0, 12)}, keeping activePipelineId=${currentActivePipelineId?.slice(0, 12) || 'null'}`,
      )
    }
  }

  ensureStreamingPlaceholder(pipelineId, messageId, threadId)

  clearUnifiedStreamTimeout(pipelineId)
  if (threadId && threadId !== pipelineId) {
    clearUnifiedStreamTimeout(threadId)
  }

  if (threadId) {
    globalWS.clearPendingAckForThread(threadId)
  }
}

/**
 * 处理流式块事件
 */
export function handleStreamChunk(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // FIX: pipeline_id 为空时 warn 并 return，不使用 _threadId fallback
    _debugLogger.warn(
      `[STREAM_CHUNK] pipeline_id missing, discarding event: _threadId=%s`,
      eventData._threadId?.slice(0, 12),
    )
    return
  }
  const messageId = extractMessageId(eventData)
  const content = eventData.content || eventData.data?.content || eventData.data?.chunk || ''
  if (!messageId) return

  // 确保目标消息存在（chunk 先于 start 到达时自动创建占位符）
  let msgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = msgs.find((m: any) => m.id === messageId)
  if (!existingMsg) {
    _debugLogger.warn(
      `[STREAM_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s totalMsgs=%d`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), msgs.length,
    )
    ensureStreamingPlaceholder(pipelineId, messageId, eventData.data?._threadId || eventData._threadId)
  }

  // 缓冲 chunk，由 RAF 统一刷写到 store（合并同帧多个 chunk 为单次更新）
  const bufferKey = `${pipelineId}::${messageId}`
  const existing = _chunkBuffer.get(bufferKey)
  if (existing) {
    existing.chunks.push(content)
  } else {
    _chunkBuffer.set(bufferKey, { chunks: [content], pipelineId, messageId })
  }
  _scheduleFlush()
}

/**
 * 处理流式结束事件
 */
export function handleStreamEnd(eventData: any) {
  // 先刷写缓冲区中的残留 chunk，再进行最终合并，避免数据丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData.data?._threadId || eventData._threadId
  const messageId = extractMessageId(eventData)

  // DEBUG: 调试日志
  _debugLogger.info(
    `[STREAM_END] pipelineId=${pipelineId?.slice(0, 12) || 'null'} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId?.slice(0, 12) || 'null'} activePipelineId=${pipelineStore.getState().activePipelineId?.slice(0, 12) || 'null'}`,
  )

  if (pipelineId) {
    clearChunkTimeout(pipelineId)
    stopPipelineStreaming(pipelineId, threadId)
    // BUG-FIX-fix_20260521_stop_button_not_cleared:
    // 问题根因: 如果 stream_end 的 pipelineId 与 activePipelineId 不一致，
    //          只清理 pipelineId 对应的 streamingTabs，但 activePipelineId 对应的可能没有清理。
    // 修复方案: 同时清理 activePipelineId 对应的 streamingTabs。
    const currentActivePipelineId = pipelineStore.getState().activePipelineId
    if (currentActivePipelineId && currentActivePipelineId !== pipelineId) {
      _debugLogger.info(
        `[STREAM_END] also clearing activePipelineId=${currentActivePipelineId.slice(0, 12)}`,
      )
      useStreamingStore.getState().setStreamingForTab(currentActivePipelineId, false)
    }
  } else {
    _debugLogger.warn(
      `[STREAM_END] pipeline_id missing, _threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId?.slice(0, 12) || 'null'}`,
    )
    // BUG-FIX-fix_20260521_stop_button_not_cleared:
    // 问题根因: stream_end 事件中 pipelineId 解析失败时，只清理了 threadId 对应的 streamingTabs。
    //          但 handleStreamStart 中已将 activePipelineId 设置为当时的 pipelineId。
    //          如果 stream_end 的 pipelineId 缺失，streamingTabs[activePipelineId] 无法被清理。
    // 修复方案: 同时清理 activePipelineId 对应的 streamingTabs。
    const currentActivePipelineId = pipelineStore.getState().activePipelineId
    if (currentActivePipelineId) {
      _debugLogger.info(
        `[STREAM_END] clearing via activePipelineId=${currentActivePipelineId.slice(0, 12)}`,
      )
      clearChunkTimeout(currentActivePipelineId)
      stopPipelineStreaming(currentActivePipelineId, threadId)
    }
    if (threadId) {
      clearChunkTimeout(threadId)
      useStreamingStore.getState().setStreamingForTab(threadId, false)
    }
    return
  }

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  // messageId 已在函数开头提取
  const fullContent = eventData?.full_content || eventData?.data?.full_content || eventData?.data?.final_content
  if (!messageId) return

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const finalContent = fullContent || msg.content || ''
  const finalThinking = (msg as any).thinking
    ? { ...(msg as any).thinking, isThinking: false }
    : undefined

  const existingBlocks = msg.contentBlocks || []
  const hasTextBlocks = existingBlocks.some((b: any) => b.type === 'text' && b.text?.trim())

  let finalBlocks: any[]
  if (hasTextBlocks) {
    finalBlocks = existingBlocks.map((block: any) => {
      if (block.type === 'thinking' && block.thinking) {
        return { ...block, thinking: { ...block.thinking, isThinking: false } }
      }
      return block
    })
  } else if (finalContent.trim()) {
    finalBlocks = reconcileContentBlocks(
      existingBlocks, finalContent, (msg as any).toolCalls, finalThinking, messageId,
    )
  } else {
    finalBlocks = existingBlocks
  }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    status: 'completed',
    content: finalContent,
    contentBlocks: finalBlocks,
    _reconciled: true,
    ...(finalThinking ? { thinking: finalThinking } : {}),
  } as any)
}

/**
 * 处理流式错误事件
 */
export function handleStreamError(eventData: any) {
  // 先刷写缓冲区，确保错误前的内容不丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData.data?._threadId || eventData._threadId

  if (pipelineId) {
    clearChunkTimeout(pipelineId)
    stopPipelineStreaming(pipelineId, threadId)
  } else if (threadId) {
    clearChunkTimeout(threadId)
    useStreamingStore.getState().stopStreamingForTab(threadId)
  }

  if (!pipelineId) return

  const messageId = extractMessageId(eventData)
  if (messageId) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'error',
    } as any)
  }

  const errorMsg = eventData?.data?.error || eventData?.error || '流式响应异常'
  useNotificationStore.getState().addNotification({
    title: '流式响应错误',
    message: typeof errorMsg === 'string' ? errorMsg : '生成过程中发生错误，请重试',
    priority: 'high',
    category: 'error',
    isBlocking: false,
  })
}

/**
 * 处理流式保活事件
 */
export function handleStreamKeepalive(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = getChunkTimeoutMessageId(pipelineId)
  if (messageId) {
    resetChunkTimeout(pipelineId, messageId)
  }
}
