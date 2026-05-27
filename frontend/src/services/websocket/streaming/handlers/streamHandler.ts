/**
 * 流式事件处理器（start / chunk / end / error / keepalive）
 *
 * 性能优化：stream_chunk 事件通过 RAF 批处理合并同一帧内的多个 chunk，
 * 将 N 次 Zustand 更新压缩为 1 次，显著降低流式输出期间的 UI 卡顿。
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

import { getChunkTimeoutMessageId, resetChunkTimeout } from '../chunkTimeout'
import { resolvePipelineId } from '../router'

import { ensureStreamingPlaceholder, extractMessageId, extractThreadId, terminatePipeline } from './utils'

const _debugLogger = loggers.websocket

// ── RAF 批处理：合并同一帧内的多个 stream_chunk 为单次 store 更新 ──

/** 每个 (pipelineId, messageId) 对应的待刷写 chunk 缓冲 */
const _chunkBuffer = new Map<string, {
  chunks: string[]
  pipelineId: string
  messageId: string
  firstSequence?: number
}>()
let _flushRafId: number | null = null

/**
 * 将缓冲区的 chunk 合并后一次性写入 store。
 * 通过 parts[] 路径写入：找到 streaming 状态的 text part 并追加内容。
 */
function _flushChunks(): void {
  _flushRafId = null
  if (_chunkBuffer.size === 0) return

  const entries = [..._chunkBuffer.values()]
  _chunkBuffer.clear()

  for (const entry of entries) {
    const combinedContent = entry.chunks.join('')
    if (!combinedContent) continue

    let partIndex = pipelineStore.getState().findStreamingPartIndex(entry.pipelineId, entry.messageId)
    if (partIndex < 0) {
      pipelineStore.getState().appendPart(entry.pipelineId, entry.messageId, {
        type: 'text',
        content: '',
        state: 'streaming',
        sequence: entry.firstSequence ?? Date.now(),
      })
      partIndex = pipelineStore.getState().findStreamingPartIndex(entry.pipelineId, entry.messageId)
    }
    if (partIndex >= 0) {
      pipelineStore.getState().appendToPart(entry.pipelineId, entry.messageId, partIndex, combinedContent)
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

  const threadId = extractThreadId(eventData)

  const currentActivePipelineId = pipelineStore.getState().activePipelineId
  _debugLogger.info(
    `[STREAM_START] pipelineId=${pipelineId.slice(0, 12)} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId.slice(0, 12)} activePipelineId=${currentActivePipelineId?.slice(0, 12) || 'null'}`,
  )

  ensureStreamingPlaceholder(pipelineId, messageId, threadId)

  if (currentActivePipelineId !== pipelineId) {
    const agentTabStore = useAgentTabStore.getState()
    const activeTab = agentTabStore.getActiveTab()
    const interactionStore = useInteractionStore.getState()

    const shouldActivatePipeline = (() => {
      // BUG-FIX-fix_20260524_cross_session_stream:
      // 问题根因: 用户在会话A发送消息后快速切换到会话B，会话A的 stream_start 事件到达时，
      //          shouldActivatePipeline 未检查管道是否属于当前活跃会话，导致 activePipelineId
      //          被错误切换回会话A的管道，会话A的消息渲染在会话B的界面中。
      // 修复方案: 在所有激活判断之前增加会话边界检查，管道不属于当前会话时禁止自动激活。
      // 影响范围: 跨会话切换时的流式消息渲染
      // 修复日期: 2026-05-24
      const pipelineMeta = pipelineStore.getState().pipelines[pipelineId]
      const activeSessionId = useSessionStore.getState().activeSessionId
      if (pipelineMeta?.sessionId && activeSessionId && pipelineMeta.sessionId !== activeSessionId) {
        return false
      }
      if (activeTab?.pipelineRunId === pipelineId) return true
      const tabIdForPipeline = agentTabStore.getTabIdByPipeline(pipelineId)
      if (tabIdForPipeline && tabIdForPipeline === agentTabStore.activeTabId) return true
      if (interactionStore.getEnteredForPipeline(pipelineId)) return true
      // BUG-FIX-fix_20260527_sub_agent_auto_activate:
      // 问题根因: 原修复仅在 !currentActivePipelineId 时检查子Agent管道，
      //          竞态条件下 sub_agent_created 尚未处理、stream_start 先到达时，
      //          pipelineTabMap 无映射且 pipelineMeta 为 null，子Agent管道被误判为主管道自动激活。
      // 修复方案: 将子Agent管道守卫提升为通用规则（不受 currentActivePipelineId 状态限制），
      //          任何有 tab 映射或 level>=2 的管道均不自动激活，
      //          子Agent管道必须通过用户点击对话按钮或人类交互工具触发才会激活。
      // 影响范围: 子Agent管道标签的自动弹出行为
      // 修复日期: 2026-05-27
      const hasTabMapping = !!agentTabStore.getTabIdByPipeline(pipelineId)
      const isSubAgentPipeline = hasTabMapping || (pipelineMeta && pipelineMeta.level > 1)
      if (isSubAgentPipeline) {
        return false
      }
      if (!currentActivePipelineId) {
        return true
      }
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
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = msgs.find((m: any) => m.id === messageId)
  if (!existingMsg) {
    _debugLogger.warn(
      `[STREAM_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s totalMsgs=%d`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), msgs.length,
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }

  // 缓冲 chunk，由 RAF 统一刷写到 store（合并同帧多个 chunk 为单次更新）
  const sequence = eventData.sequence ?? eventData.data?.sequence
  const bufferKey = `${pipelineId}::${messageId}`
  const existing = _chunkBuffer.get(bufferKey)
  if (existing) {
    existing.chunks.push(content)
  } else {
    _chunkBuffer.set(bufferKey, { chunks: [content], pipelineId, messageId, firstSequence: sequence })
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
  const threadId = extractThreadId(eventData)
  const messageId = extractMessageId(eventData)

  // DEBUG: 调试日志
  _debugLogger.info(
    `[STREAM_END] pipelineId=${pipelineId?.slice(0, 12) || 'null'} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId?.slice(0, 12) || 'null'} activePipelineId=${pipelineStore.getState().activePipelineId?.slice(0, 12) || 'null'}`,
  )

  if (pipelineId) {
    // BUG-FIX-fix_20260522: 标记管道已终止，防止 ensureStreamingPlaceholder 重新启动
    terminatePipeline(pipelineId, threadId)
    // BUG-FIX-fix_20260522_stream_end_over_cleanup:
    // stream_end 时需要确保所有关联的 streamingTabs 都被清理，
    // 包括 pipelineId、threadId 和 activePipelineId。
    const currentActivePipelineId = pipelineStore.getState().activePipelineId
    if (currentActivePipelineId && currentActivePipelineId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(currentActivePipelineId, false)
    }
    if (threadId && threadId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(threadId, false)
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
      terminatePipeline(currentActivePipelineId, threadId)
    }
    if (threadId) {
      useStreamingStore.getState().setStreamingForTab(threadId, false)
    }
    return
  }

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  if (!messageId) return

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    status: 'completed',
  } as any)
  pipelineStore.getState().finalizeMessage(pipelineId, messageId)
}

/**
 * 处理流式错误事件
 */
export function handleStreamError(eventData: any) {
  // 先刷写缓冲区，确保错误前的内容不丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    // BUG-FIX-fix_20260522: 标记管道已终止（错误），防止 ensureStreamingPlaceholder 重新启动
    terminatePipeline(pipelineId, threadId)
  } else if (threadId) {
    useStreamingStore.getState().stopStreamingForTab(threadId)
  }

  if (!pipelineId) return

  const messageId = extractMessageId(eventData)
  if (messageId) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'error',
    } as any)

    // 将所有 streaming 状态的 part 标记为 done/error
    const store = pipelineStore.getState()
    const msg = store.getMessages(pipelineId)?.find((m: any) => m.id === messageId)
    if (msg?.parts) {
      msg.parts.forEach((p: any, i: number) => {
        if (p.type === 'text' || p.type === 'thinking') {
          if (p.state === 'streaming') {
            store.updatePart(pipelineId, messageId, i, { state: 'done' })
          }
        }
        if (p.type === 'tool_call') {
          if (p.state === 'streaming' || p.state === 'calling') {
            store.updatePart(pipelineId, messageId, i, { state: 'error' })
          }
        }
      })
    }
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
