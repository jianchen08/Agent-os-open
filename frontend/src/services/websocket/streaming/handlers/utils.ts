/**
 * 流式事件处理器公共工具函数
 *
 * 统一抽取的消息 ID 提取、流式占位符创建、Streaming 状态管理，
 * 消除各 handler 中的重复代码，确保 pipeline_id 唯一路由原则。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { clearChunkTimeout } from '../chunkTimeout'
import { resolvePipelineId } from '../router'

// ── 管道终止标记：防止 ensureStreamingPlaceholder 在流式结束后重新启动 ──

/** 记录已经终止的管道（stream_end / stream_error / chunk 超时 / new_message），防止 ensureStreamingPlaceholder 重新启动 */
const _terminatedPipelines = new Set<string>()

/** 标记管道已终止（stream_end / stream_error / chunk 超时 / new_message 时调用） */
export function markPipelineTerminated(pipelineId: string): void {
  _terminatedPipelines.add(pipelineId)
}

/** 清除管道终止标记（stream_start 时调用，表示新一轮流式开始） */
export function clearPipelineTerminated(pipelineId: string): void {
  _terminatedPipelines.delete(pipelineId)
}

/** 检查管道是否已终止 */
export function isPipelineTerminated(pipelineId: string): boolean {
  return _terminatedPipelines.has(pipelineId)
}

/**
 * 从事件数据中提取消息 ID
 *
 * 统一处理 message_id 的多种来源，避免各 handler 重复写
 * `eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id` 模式。
 *
 * @param eventData - WebSocket 事件数据（顶层或嵌套 data）
 * @returns 消息 ID 字符串，找不到时返回 null
 */
export function extractMessageId(eventData: any): string | null {
  if (!eventData) return null
  return (
    eventData.message_id
    || eventData.data?.message_id
    || eventData.data?.ai_message_id
    || null
  )
}

/**
 * 统一启动管道流式状态（pipelineStore + streamingStore）
 *
 * pipelineStore.streamingState 为唯一数据源，
 * streamingStore.setStreamingForTab 仅在 pipelineStore 操作后统一调用一次。
 * threadId 用于 streamingStore 的双 key 配对（UI tab 指示器），不参与消息路由。
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param messageId - 正在流式传输的消息 ID
 * @param threadId - 可选的会话 ID，用于 streamingStore tab 配对
 */
export function startPipelineStreaming(
  pipelineId: string,
  messageId: string,
  threadId?: string,
): void {
  pipelineStore.getState().startStreaming(pipelineId, messageId)
  useStreamingStore.getState().setStreamingForTab(pipelineId, true)

  // FIX: threadId 仅用于 streamingStore 双 key 配对，不参与消息路由
  if (threadId && threadId !== pipelineId) {
    useStreamingStore.getState().setStreamingForTab(threadId, true)
  }
}

/**
 * 统一停止管道流式状态（pipelineStore + streamingStore）
 *
 * pipelineStore.stopStreaming 内部已调用 streamingStore.setStreamingForTab(pipelineId, false)，
 * 此处仅额外清理 threadId 对应的 tab 状态。
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param threadId - 可选的会话 ID，用于清理 streamingStore tab 配对
 */
export function stopPipelineStreaming(pipelineId: string, threadId?: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)

  // pipelineStore.stopStreaming 已清理 pipelineId 的 tab 状态，仅需额外清理 threadId
  if (threadId && threadId !== pipelineId) {
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }
}

/**
 * 确保流式占位符消息存在
 *
 * 合并 startStreaming + setStreamingForTab + addMessage 三步操作，
 * 当 stream_start 丢失或 chunk 先于 start 到达时自动创建占位符。
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param messageId - 消息 ID
 * @param threadId - 可选的会话 ID，用于 streamingStore tab 配对
 */
export function ensureStreamingPlaceholder(
  pipelineId: string,
  messageId: string,
  threadId?: string,
): void {
  // BUG-FIX-fix_20260522: 如果管道已被终止（stream_end/chunk超时/stream_error/new_message），
  // 不重新启动 streaming，防止停止按钮反复出现
  if (isPipelineTerminated(pipelineId)) {
    return
  }

  startPipelineStreaming(pipelineId, messageId, threadId)

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const nextSeq = existingMsgs.reduce(
    (max: number, m: any) => Math.max(max, m.sequence ?? 0), 0,
  ) + 1

  pipelineStore.getState().addMessage(pipelineId, {
    id: messageId,
    sessionId: threadId || '',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: nextSeq,
    status: 'streaming',
  } as any)
}

/**
 * 从事件数据中提取 threadId
 *
 * 统一处理 `eventData.data?._threadId || eventData._threadId` 模式。
 */
export function extractThreadId(eventData: any): string | undefined {
  return eventData.data?._threadId || eventData._threadId
}

/**
 * 终止管道：封装 markPipelineTerminated + clearChunkTimeout + stopPipelineStreaming 三件套
 *
 * 在 stream_end / stream_error / chunk超时 等终止场景中复用。
 */
export function terminatePipeline(pipelineId: string, threadId?: string): void {
  markPipelineTerminated(pipelineId)
  clearChunkTimeout(pipelineId)
  stopPipelineStreaming(pipelineId, threadId)
}

/**
 * 清理消息中 streaming 状态的 parts（将 state 设为 done）
 *
 * 返回浅拷贝，不修改原始消息对象。如果 parts 无 streaming 状态，直接返回原对象。
 */
export function clearStreamingParts(msg: any): any {
  const parts = msg.parts || []
  const hasStreaming = parts.some((p: any) => p.state === 'streaming')
  if (hasStreaming) {
    return {
      ...msg,
      parts: parts.map((p: any) =>
        p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
      ),
    }
  }
  return msg
}

/**
 * 解析 pipelineId 并执行空值守卫 + warn 日志
 *
 * 返回 null 表示 pipelineId 为空，调用方应跳过处理。
 */
export function resolveRequiredPipelineId(eventData: any, context: string): string | null {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    console.warn(`[streaming] ${context}: pipelineId 为空，跳过事件`)
    return null
  }
  return pipelineId
}
