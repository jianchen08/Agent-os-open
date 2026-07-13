/** 流式事件处理器（start / chunk / end / error / keepalive） 性能优化：stream_chunk 事件通过 RAF 批处理合并同一帧内的多个 chunk， */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { ensureStreamingPlaceholder, extractMessageId, extractThreadId, mergeStreamingParts, terminatePipeline } from './utils'

const _debugLogger = loggers.websocket

// ── RAF 批处理：合并同一帧内的多个 chunk 为单次 store 更新 ──
// stream_chunk（正文）和 thinking_chunk（思考）共用同一套缓冲 + RAF，
// 避免每个 chunk 同步触发 store 更新 → React 重渲染阻塞主线程。
// 思考 chunk 若同步写入，每个都阻塞一帧，导致思考"匀速逐字慢"且正文
// chunk 积压在 buffer 中等主线程空闲才一次性 flush（"一股脑全渲染"）。

type ChunkPartType = 'text' | 'thinking'

/** 每个 (pipelineId, messageId, partType) 对应的待刷写 chunk 缓冲 */
const _chunkBuffer = new Map<string, {
  chunks: string[]
  pipelineId: string
  messageId: string
  partType: ChunkPartType
}>()
let _flushRafId: number | null = null

/** 将缓冲区的 chunk 合并后一次性写入 store。 按 partType 路由到 text/thinking part。 */
function _flushChunks(): void {
  _flushRafId = null
  if (_chunkBuffer.size === 0) return

  const entries = [..._chunkBuffer.values()]
  _chunkBuffer.clear()

  for (const entry of entries) {
    const combinedContent = entry.chunks.join('')
    if (!combinedContent) continue

    if (entry.partType === 'thinking') {
      // thinking part 用 findLastPartIndex 精确路由（与 thinkingHandler 一致）
      let partIndex = pipelineStore.getState().findLastPartIndex(entry.pipelineId, entry.messageId, 'thinking')
      if (partIndex < 0) {
        pipelineStore.getState().appendPart(entry.pipelineId, entry.messageId, {
          type: 'thinking',
          content: '',
          state: 'streaming',
        })
        partIndex = pipelineStore.getState().findLastPartIndex(entry.pipelineId, entry.messageId, 'thinking')
      }
      if (partIndex >= 0) {
        pipelineStore.getState().appendToPart(entry.pipelineId, entry.messageId, partIndex, combinedContent)
      }
    } else {
      // text part 用 findStreamingPartIndex（仅匹配 text，避免误入 thinking part）
      let partIndex = pipelineStore.getState().findStreamingPartIndex(entry.pipelineId, entry.messageId)
      if (partIndex < 0) {
        pipelineStore.getState().appendPart(entry.pipelineId, entry.messageId, {
          type: 'text',
          content: '',
          state: 'streaming',
          // part 渲染按数组顺序（= 追加顺序 = 接收顺序），不分配 sequence。
        })
        partIndex = pipelineStore.getState().findStreamingPartIndex(entry.pipelineId, entry.messageId)
      }
      if (partIndex >= 0) {
        pipelineStore.getState().appendToPart(entry.pipelineId, entry.messageId, partIndex, combinedContent)
      }
    }
  }
}

/** 调度 RAF 刷写（幂等，同一帧内多次调用只触发一次） */
function _scheduleFlush(): void {
  if (_flushRafId === null) {
    _flushRafId = requestAnimationFrame(_flushChunks)
  }
}

/** 缓冲一个 chunk，等待 RAF 批量刷写。 stream_chunk 和 thinking_chunk 共用。 */
export function bufferChunk(pipelineId: string, messageId: string, content: string, partType: ChunkPartType): void {
  const bufferKey = `${pipelineId}::${messageId}::${partType}`
  const existing = _chunkBuffer.get(bufferKey)
  if (existing) {
    existing.chunks.push(content)
  } else {
    _chunkBuffer.set(bufferKey, { chunks: [content], pipelineId, messageId, partType })
  }
  _scheduleFlush()
}

/** 立即刷写缓冲区。 streamEnd / streamError / thinkingEnd 必须在 reconcile 之前调用此方法， */
export function flushStreamChunkBuffer(): void {
  if (_flushRafId !== null) {
    cancelAnimationFrame(_flushRafId)
    _flushRafId = null
  }
  _flushChunks()
}

/** 处理流式开始事件 */
export function handleStreamStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // pipeline_id 为空时 warn 并 return，_threadId 不参与消息路由
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

  // 提取后端返回的真实 sequence
  const backendSeq = eventData.sequence ?? eventData.data?.sequence
  ensureStreamingPlaceholder(pipelineId, messageId, threadId, backendSeq)

  if (currentActivePipelineId === pipelineId) return

  const agentTabStore = useAgentTabStore.getState()
  const activeTab = agentTabStore.getActiveTab()
  if (activeTab?.pipelineRunId === pipelineId) {
    pipelineStore.getState().activatePipeline(pipelineId)
  }
}

/** 处理流式块事件 */
export function handleStreamChunk(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // pipeline_id 为空时 warn 并 return，不使用 _threadId fallback
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
  bufferChunk(pipelineId, messageId, content, 'text')
}

/** 处理流式结束事件 */
export function handleStreamEnd(eventData: any) {
  // 先刷写缓冲区中的残留 chunk，再进行最终合并，避免数据丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)
  const messageId = extractMessageId(eventData)

  _debugLogger.info(
    `[STREAM_END] pipelineId=${pipelineId?.slice(0, 12) || 'null'} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId?.slice(0, 12) || 'null'} activePipelineId=${pipelineStore.getState().activePipelineId?.slice(0, 12) || 'null'}`,
  )

  if (pipelineId) {
    terminatePipeline(pipelineId, threadId)
    // 子管道 stream_end 携带的 threadId 与 pipelineId 不同时，单独终止该 threadId 的流。
    if (threadId && threadId !== pipelineId) {
      pipelineStore.getState().stopStreaming(threadId)
    }

    if (messageId) {
      const msgs = pipelineStore.getState().getMessages(pipelineId)
      const msg = msgs.find((m: any) => m.id === messageId)

      if (msg) {
        // msg 存在：合并后端权威 parts/sequence，收尾占位。
        // 同步后端权威 sequence（final_sequence）
        // stream_start 不携带 sequence，占位消息的 sequence 是前端自算的 localMax+1，
        // 与后端真实序号不一致。stream_end 携带 final_sequence，必须在此同步到占位消息，
        // 否则后续 initFromAPI（刷新/切Tab/补漏）按 role::seq 指纹去重时会失败，
        // 把同一逻辑消息识别为两条 → 末尾气泡重复渲染。
        // 与 handleNewMessage 的 sequence 同步路径对齐，消除两条终止路径的不对称。
        const finalSeq = eventData?.data?.final_sequence ?? eventData?.final_sequence
        if (finalSeq != null && finalSeq !== msg.sequence) {
          pipelineStore.getState().updateMessage(pipelineId, messageId, {
            sequence: finalSeq,
          } as any)
        }

        // 合并而非覆盖：本地有实质内容就优先保留本地，serverParts 仅作兜底（详见 mergeStreamingParts）。
        const serverParts = eventData?.data?.parts
        const localParts = msg.parts || []
        if (serverParts && Array.isArray(serverParts) && serverParts.length > 0) {
          const fullContent = eventData?.data?.full_content
          const { parts: finalParts, content } = mergeStreamingParts(
            localParts, serverParts, fullContent, msg.content,
          )
          const updatePayload: any = {
            parts: finalParts,
            content,
            status: 'completed',
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
            // 空内容兜底：补一条 warning 并收尾，避免占位消息卡在 streaming。
            pipelineStore.getState().appendPart(pipelineId, messageId, {
              type: 'system',
              content: 'AI 回复内容为空，请重试',
              level: 'warning',
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
      } else {
        // stream_end 找不到本地消息：说明 stream_start/chunk 在断线期间丢失，本地无对应占位。
        // 后端已完成并持久化，由重连/重进入时的统一重新加载（useRealtimeEvents 的 initFromAPI
        // 对账）拉取权威内容。此处不主动发请求（handler 是纯事件处理，不触发 HTTP）。
        _debugLogger.warn(
          '[STREAM_END] 本地无对应消息（断线期间 stream_start 丢失），将由重新加载对账: pipeline=%s msgId=%s',
          pipelineId?.slice(0, 12), messageId?.slice(0, 12),
        )
      }
    }

  } else {
    _debugLogger.warn(
      `[STREAM_END] pipeline_id missing, _threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId?.slice(0, 12) || 'null'}`,
    )
    // 如果 stream_end 的 pipelineId 缺失，streamingTabs[activePipelineId] 无法被清理。
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

/** 处理流式错误事件 */
export function handleStreamError(eventData: any) {
  // 先刷写缓冲区，确保错误前的内容不丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    // 标记管道已终止（错误），防止 ensureStreamingPlaceholder 重新启动
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

/** 处理通用 ERROR 事件（后端通过 WS error 类型发送的全局错误） 与 STREAM_ERROR 不同：通用 ERROR 不绑定特定流式管道， */
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

/** 处理流式保活事件 */
export function handleStreamKeepalive(eventData: any) {
  // keepalive 是"连接保活"信号，同时检查是否有卡死的 streaming 管道
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return

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
