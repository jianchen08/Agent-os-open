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
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
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
    terminatePipeline(pipelineId, threadId)
    // BUG-FIX-fix_20260616_154200_msg_id:
    // 问题根因: 原 handleStreamEnd 在 stream_end 的 pipelineId 与 activePipelineId 不一致时，
    //   会调用 stopStreaming(activePipelineId) 清理"当前活动管道"的流式状态。但在子管道
    //   （如 trigger_review / 分派子任务）场景下，子管道的 stream_end 携带子管道 pipelineId，
    //   而 activePipelineId 是用户正在查看的主管道。这段逻辑会错误地把主管道的占位消息
    //   标记为 completed（内容仍为空）并清空 streamingState，导致主管道后续的 stream_chunk
    //   /stream_end 无法再更新该占位消息，用户看到空白气泡，刷新后从 API 重新拉取才显示。
    // 修复方案: stream_end 只清理自身 pipelineId 的流式状态（terminatePipeline 已做），
    //   不再清理 activePipelineId。子管道的结束不应影响主管道。
    if (threadId && threadId !== pipelineId) {
      pipelineStore.getState().stopStreaming(threadId)
    }

    if (messageId) {
      const msgs = pipelineStore.getState().getMessages(pipelineId)
      const msg = msgs.find((m: any) => m.id === messageId)

      if (msg) {
        // 后端发送了完整 parts[] → 合并而非覆盖
        // BUG-FIX-fix_20260617_stream_end_overwrite:
        // 问题根因: stream_end 的 serverParts 只包含最后一轮 iteration 的内容（后端 state
        //   每轮覆盖）。用 serverParts 完整替换会丢失流式过程中增量构建的前几轮内容。
        // 修复方案: 本地 parts 数量更多时保留本地（流式增量），否则用 server（权威）。
        const serverParts = eventData?.data?.parts
        const localParts = msg.parts || []
        if (serverParts && Array.isArray(serverParts) && serverParts.length > 0) {
          const fullContent = eventData?.data?.full_content
          const finalParts = localParts.length > serverParts.length ? localParts : serverParts
          const updatePayload: any = {
            parts: finalParts,
            status: 'completed',
          }
          if (fullContent != null && fullContent !== '' && fullContent.length > (msg.content || '').length) {
            updatePayload.content = fullContent
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
          } else {
            // BUG-FIX-fix_20260614_empty_stream_end_stuck:
            // 问题根因: 后端发空 stream_end（无 parts、无 content，如引擎异常退出但未发 stream_error）
            //   时，占位消息 content='' 且 parts=[]，hasContent=false，原代码什么都不做，
            //   占位消息永远卡在 status='streaming'，用户看到空气泡（bug 2 前端侧）。
            // 修复方案: 与 BUG-FIX-M02 一致，追加 system warning part + 标记消息 + 通知用户，
            //   确保占位消息不会无声卡死。
            pipelineStore.getState().appendPart(pipelineId, messageId, {
              type: 'system',
              content: 'AI 回复内容为空，请重试',
              level: 'warning',
              sequence: Date.now(),
            })
            pipelineStore.getState().updateMessage(pipelineId, messageId, {
              status: 'completed',
            } as any)
            useNotificationStore.getState().addNotification({
              title: '回复内容为空',
              message: 'AI 生成的回复内容为空，请重新发送或重试',
              priority: 'normal',
              category: 'alert',
              isBlocking: false,
            })
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
      pipelineStore.getState().stopStreaming(threadId)
    }
    return
  }

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  // REQ-28: 首次 AI 回复完成后自动重命名会话
  if (threadId && pipelineId) {
    useSessionListStore.getState().autoRenameSessionIfNeeded(threadId, pipelineId)
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
    pipelineStore.getState().stopStreaming(threadId)
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
 * 处理通用 ERROR 事件（后端通过 WS error 类型发送的全局错误）
 *
 * 与 STREAM_ERROR 不同：通用 ERROR 不绑定特定流式管道，
 * 可能由后端在请求级 / 会话级失败时发送，需要兜底通知用户并终止相关 streaming。
 *
 * BUG-FIX-M01: 通用 ERROR 事件无前端 handler
 * 问题根因: WS_SERVER_EVENTS.ERROR 已定义但 streaming/index.ts 未注册 handler，
 *           后端发送的 error 事件被静默丢弃，用户无法感知错误。
 * 修复方案: 新增 handleGlobalError，解析多字段错误信息（error/message/data.error），
 *           通过 notificationStore 展示可读错误，并终止相关 streaming 状态。
 * 影响范围: 后端通用 error 事件的用户可见性
 */
export function handleGlobalError(eventData: any) {
  // 先刷写缓冲区，确保错误前的内容不丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  // 终止相关 streaming 状态，避免 UI 卡在生成中
  if (pipelineId) {
    terminatePipeline(pipelineId, threadId)
  } else if (threadId) {
    pipelineStore.getState().stopStreaming(threadId)
  }

  // 解析错误信息（兼容 error / message / data.error / data.message 多种字段）
  const rawError =
    eventData?.error
    || eventData?.message
    || eventData?.data?.error
    || eventData?.data?.message
    || ''
  const errorMsg =
    typeof rawError === 'string' && rawError.trim()
      ? rawError.trim()
      : '服务器返回错误，请稍后重试'

  useNotificationStore.getState().addNotification({
    title: '请求失败',
    message: errorMsg,
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
  // keepalive 是"连接保活"信号，同时检查是否有卡死的 streaming 管道
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return

  // BUG-FIX-fix_20260617_streaming_timeout_watchdog:
  // 问题根因: 删除 chunkTimeout 后，若后端终止事件（stream_end/state_change）丢失，
  //   streamingState 永远不被清理，UI 永久转圈。
  // 修复方案: 利用 keepalive 做轻量级 watchdog，检查所有 streaming 管道，
  //   超过 180s 的管道强制终止。keepalive 每 ~30s 发一次，足够及时。
  // 影响范围: 后端事件丢失时的 streaming 状态恢复
  // 修复日期: 2026-06-17
  const STREAMING_TIMEOUT_MS = 180_000 // 3 分钟
  const now = Date.now()
  const streamingState = pipelineStore.getState().streamingState
  for (const [pid, info] of Object.entries(streamingState)) {
    const startedAt = (info as any)?.startedAt
    if (startedAt && (now - startedAt) > STREAMING_TIMEOUT_MS) {
      _debugLogger.warn(
        '[STREAMING-WATCHDOG] 管道 %s 流式超时（%ds），强制终止',
        pid.slice(0, 12), Math.round((now - startedAt) / 1000),
      )
      terminatePipeline(pid, undefined)
    }
  }
}
