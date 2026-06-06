/**
 * 流式事件处理器（start / chunk / end / error / keepalive）
 *
 * 性能优化：stream_chunk 事件通过 RAF 批处理合并同一帧内的多个 chunk，
 * 将 N 次 Zustand 更新压缩为 1 次，显著降低流式输出期间的 UI 卡顿。
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

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

  const pipelineState = pipelineStore.getState()
  if (!pipelineState.pipelines[pipelineId]) {
    const sessionId = threadId || useSessionStore.getState().activeSessionId || ''
    pipelineState.registerPipeline({ pipelineId, sessionId })
    _debugLogger.info(
      `[STREAM_START] auto-registered unknown pipeline: pipelineId=%s sessionId=%s`,
      pipelineId.slice(0, 12), sessionId?.slice(0, 12) || 'null',
    )
  }

  const currentActivePipelineId = pipelineStore.getState().activePipelineId
  _debugLogger.info(
    `[STREAM_START] pipelineId=${pipelineId.slice(0, 12)} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId.slice(0, 12)} activePipelineId=${currentActivePipelineId?.slice(0, 12) || 'null'}`,
  )

  // BUG-FIX-fix_20260529_msg_order: 提取后端返回的真实 sequence
  // 问题根因: 前端自算 sequence 与后端不一致
  // 修复方案: 从 WS 事件中提取后端 sequence，传递给 ensureStreamingPlaceholder
  const backendSeq = eventData.sequence ?? eventData.data?.sequence
  ensureStreamingPlaceholder(pipelineId, messageId, threadId, backendSeq)

  if (currentActivePipelineId === pipelineId) return

  const agentTabStore = useAgentTabStore.getState()
  const activeTab = agentTabStore.getActiveTab()
  if (activeTab?.pipelineRunId === pipelineId) {
    pipelineStore.getState().activatePipeline(pipelineId)
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
    // DEBUG: 打印终止前 streamingState 现状
    const _beforeStream = pipelineStore.getState().streamingState
    const _beforeKeys = Object.keys(_beforeStream).filter(k => _beforeStream[k]?.isStreaming)
    console.warn('[STREAM_END] terminate前 streaming管道: %s activePipeline: %s', 
      _beforeKeys.join(','), pipelineStore.getState().activePipelineId?.slice(0,12))
    terminatePipeline(pipelineId, threadId)
    const currentActivePipelineId = pipelineStore.getState().activePipelineId
    if (currentActivePipelineId && currentActivePipelineId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(currentActivePipelineId, false)
    }
    if (threadId && threadId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(threadId, false)
    }

    if (messageId) {
      const msgs = pipelineStore.getState().getMessages(pipelineId)
      const msg = msgs.find((m: any) => m.id === messageId)
      if (msg) {
        // 后端发送了完整 parts[] → 用权威版本完整替换
        const serverParts = eventData?.data?.parts
        if (serverParts && Array.isArray(serverParts)) {
          console.warn(
            `[STREAM_END] 收到后端 parts[${serverParts.length}]: %s`,
            serverParts.map((p: any) => `${p.type}/${p.state}`).join(','),
          )
          // 仅当 full_content 非空时才更新 content，避免空白消息
          const fullContent = eventData?.data?.full_content
          const updatePayload: any = {
            parts: serverParts,
            status: 'completed',
          }
          if (fullContent != null && fullContent !== '') {
            updatePayload.content = fullContent
          } else if (!msg.content) {
            console.warn('[STREAM_END] full_content 和 msg.content 均为空，消息将无内容', {
              messageId,
              pipelineId,
            })
          }
          pipelineStore.getState().updateMessage(pipelineId, messageId, updatePayload)
        } else {
          // fallback: 后端未发 parts，走原有 finalizeMessage
          const hasContent = (msg.content || '').length > 0 || (msg.parts || []).length > 0
          if (hasContent) {
            pipelineStore.getState().finalizeMessage(pipelineId, messageId)
            if (msg.status === 'streaming') {
              pipelineStore.getState().updateMessage(pipelineId, messageId, {
                status: 'completed',
              } as any)
            }
          }
        }
      }
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
 *
 * BUG-FIX-fix_20260605_stuck_streaming_on_keepalive:
 * 问题根因: keepalive 在此 handler 内部调用 resetChunkTimeout，导致后端 LLM 卡住、
 *          keepalive 仍持续发送时，前端永远不会触发内容活跃超时，streamingState 永远为 true。
 * 修复方案: chunkTimeout 已删除，keepalive 不再重置任何计时器。LLM 异常时由后端
 *          call_timeout 触发 stream_end 终止事件清理状态。
 * 影响范围: 流式输出在 LLM 卡住时的状态恢复
 * 修复日期: 2026-06-05
 */
export function handleStreamKeepalive(eventData: any) {
  // keepalive 是"连接保活"信号，仅做存在性检查，不修改任何状态
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
}
