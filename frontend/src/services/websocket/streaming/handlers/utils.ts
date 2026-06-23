/**
 * 流式事件处理器公共工具函数
 *
 * 统一抽取的消息 ID 提取、流式占位符创建、Streaming 状态管理，
 * 消除各 handler 中的重复代码，确保 pipeline_id 唯一路由原则。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

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
 * 统一启动管道流式状态
 *
 * pipelineStore.streamingState 是唯一数据源。
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param messageId - 正在流式传输的消息 ID
 */
export function startPipelineStreaming(
  pipelineId: string,
  messageId: string,
): void {
  pipelineStore.getState().startStreaming(pipelineId, messageId)
}

/**
 * 停止管道流式传输
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param threadId - 可选的会话 ID，threadId 与 pipelineId 不同时一并清理
 */
export function stopPipelineStreaming(pipelineId: string, threadId?: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)
  if (threadId && threadId !== pipelineId) {
    pipelineStore.getState().stopStreaming(threadId)
  }
}

/**
 * 分配下一个 sequence 值。
 * - 后端消息：直接使用后端 sequence，但不小于本地已有最大值（防止后端计数器未续接）
 * - 用户消息：使用本地最大值 + 1（乐观更新，等后端覆盖）
 *
 * @param pipelineId - 管道 ID
 * @param backendSequence - 后端返回的真实 sequence（WS 事件携带）
 */
export function allocateNextSequence(pipelineId: string, backendSequence?: number): number {
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const localMax = existingMsgs.reduce(
    (max: number, m: any) => Math.max(max, m.sequence ?? 0), 0,
  )
  if (backendSequence != null && backendSequence > 0) {
    return Math.max(backendSequence, localMax + 1)
  }
  return localMax + 1
}

/**
 * 确保流式占位符消息存在
 *
 * 合并 startStreaming + setStreamingForTab + addMessage 三步操作，
 * 当 stream_start 丢失或 chunk 先于 start 到达时自动创建占位符。
 *
 * 同时清理同管道中旧的 streaming 占位消息（引擎唤醒/reset_for_new_turn 后
 * message_id 变化，旧占位消息残留会导致空气泡）。
 */
export function ensureStreamingPlaceholder(
  pipelineId: string,
  messageId: string,
  threadId?: string,
  backendSequence?: number,
): void {
  startPipelineStreaming(pipelineId, messageId, threadId)

  const store = pipelineStore.getState()
  const existing = store.getMessages(pipelineId)
  for (const msg of existing) {
    if (
      msg.role === 'assistant'
      && msg.status === 'streaming'
      && msg.id !== messageId
    ) {
      // BUG-FIX-fix_20260603_stale_streaming_cleanup:
      // 问题根因: 旧 streaming 占位符的清理逻辑只检查了 content/parts 是否有内容，
      //   但如果 parts 中只有 tool_call 且处于 calling 状态（未收到 tool_result），
      //   这些残留消息被标记 completed 后会与新的流式消息合并，造成渲染混乱。
      // 修复方案: 检查 tool_call parts 的解析状态。
      //   - 有未解析的 tool_call（calling）→ remove（不完整消息，直接丢弃）
      //   - 所有 tool_call 已解析 + 有内容 → 标记 completed 保留
      //   - 完全无内容 → remove
      // 影响范围: 流式过程切换时旧占位符的清理
      // 修复日期: 2026-06-03
      const parts = msg.parts || []
      const hasTextContent = (msg.content || '').length > 0
      const hasParts = parts.length > 0
      const unresolvedToolCalls = parts.some(
        (p: any) => p.type === 'tool_call' && (p.state === 'calling' || p.state === 'streaming')
      )
      const resolvedParts = parts.filter(
        (p: any) => p.type !== 'tool_call' || (p.state !== 'calling' && p.state !== 'streaming')
      )

      if (unresolvedToolCalls) {
        // 有未解析的 tool_call → 消息不完整，直接移除
        store.removeMessage(pipelineId, msg.id)
      } else if (hasTextContent || resolvedParts.length > 0) {
        // 有完整内容 → 保留但标记 completed，同时确保 tool parts 为 done
        const finalizedParts = resolvedParts.map((p: any) =>
          p.type === 'tool_call' ? { ...p, state: 'done' as const } : p
        )
        store.updateMessage(pipelineId, msg.id, {
          status: 'completed',
          parts: finalizedParts.length > 0 ? finalizedParts : undefined,
        } as any)
      } else {
        // 完全空消息 → 移除
        store.removeMessage(pipelineId, msg.id)
      }
    }
  }

  // BUG-FIX-fix_20260615_user_msg_order:
  // assistant 占位消息也要走本地 sequence 计数器（Math.max(后端 seq, 本地 max+1)），
  // 否则后端 seq（小数字）会小于已分配的 user 消息 seq → assistant 排到 user 之前。
  const placeholderSeq = allocateNextSequence(pipelineId, backendSequence)

  store.addMessage(pipelineId, {
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
 * 终止管道：清理 streamingState
 *
 * 仅在 stream_end / stream_error 等终止事件到达时调用。
 * 不再做超时兜底（chunkTimeout 已删除），后端必须主动发终止事件。
 */
export function terminatePipeline(pipelineId: string, threadId?: string): void {
  stopPipelineStreaming(pipelineId, threadId)
}

/**
 * 解析 pipelineId 并执行空值守卫 + warn 日志
 *
 * 返回 null 表示 pipelineId 为空，调用方应跳过处理。
 */
export function resolveRequiredPipelineId(eventData: any, context: string): string | null {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // BUG-FIX-M03: WS handler 层 console 残留
    // 问题根因: pipelineId 空值守卫用 console.warn 记录。
    // 修复方案: 改用正式 logger.warn。
    loggers.websocket.warn('[streaming] %s: pipelineId 为空，跳过事件', context)
    return null
  }
  return pipelineId
}
