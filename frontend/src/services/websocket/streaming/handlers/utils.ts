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
 * 停止管道流式传输，同步清理 streamingStore 的 tab 状态
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param threadId - 可选的会话 ID，用于清理 streamingStore tab 配对
 */
export function stopPipelineStreaming(pipelineId: string, threadId?: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)
  useStreamingStore.getState().setStreamingForTab(pipelineId, false)

  if (threadId && threadId !== pipelineId) {
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }
}

/**
 * BUG-FIX-fix_20260529_msg_order:
 * 统一分配下一个 sequence 值，优先使用后端返回的真实 sequence。
 *
 * 问题根因: 前端自算 sequence (max+5000) 与后端真实 sequence 不一致，
 *          导致消息排序错乱（后端已增加共享 Pipeline Sequence Allocator）。
 * 修复方案: 后端 WS 事件现在携带真实 sequence，优先使用；fallback 到自算。
 * 影响范围: 所有客户端分配 sequence 的场景
 * 修复日期: 2026-05-29
 *
 * @param pipelineId - 管道 ID
 * @param backendSequence - 可选的后端返回的真实 sequence 值
 * @returns 后端 sequence（有效时）或 当前管道中最大 sequence + 5000
 */
export function allocateNextSequence(pipelineId: string, backendSequence?: number): number {
  // BUG-FIX-fix_20260529_msg_order: 优先使用后端返回的真实 sequence
  // 问题根因: 前端自算 sequence 与后端不一致
  // 修复方案: 后端现在携带真实 sequence，优先使用
  if (backendSequence != null && backendSequence > 0) {
    return backendSequence
  }
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  return existingMsgs.reduce(
    (max: number, m: any) => Math.max(max, m.sequence ?? 0), 0,
  ) + 5000
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
 * @param backendSequence - 可选的后端返回的真实 sequence 值
 */
export function ensureStreamingPlaceholder(
  pipelineId: string,
  messageId: string,
  threadId?: string,
  backendSequence?: number,
): void {
  startPipelineStreaming(pipelineId, messageId, threadId)

  // BUG-FIX-fix_20260529_msg_order: 优先使用后端返回的真实 sequence
  // 问题根因: 前端自算 sequence 与后端不一致
  // 修复方案: 后端 WS 事件现在携带真实 sequence，透传给 allocateNextSequence
  const placeholderSeq = allocateNextSequence(pipelineId, backendSequence)

  pipelineStore.getState().addMessage(pipelineId, {
    id: messageId,
    sessionId: threadId || '',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: placeholderSeq,
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
 * 终止管道：封装 clearChunkTimeout + stopPipelineStreaming
 *
 * 在 stream_end / stream_error / chunk超时 等终止场景中复用。
 */
export function terminatePipeline(pipelineId: string, threadId?: string): void {
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
