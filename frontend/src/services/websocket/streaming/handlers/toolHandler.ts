/** 工具调用事件处理器（start / result） 仅使用 parts[] 统一路径，已移除旧 toolCalls / contentBlocks 兼容代码。 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/** 处理工具调用开始事件 向 parts[] 追加一个 tool_call part；若 call_id 缺失则跳过（等数据完整再渲染）。 */
export function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  // 没有 call_id 无法唯一定位和去重，跳过等数据完整
  if (!callId) {
    _debugLogger.debug(
      `[TOOL_START] skipped (no call_id): msgId=%s pipelineId=%s`,
      messageId?.slice(0, 12), pipelineId?.slice(0, 8),
    )
    return
  }

  const toolName = eventData.tool_name || eventData.data?.tool_name || ''
  if (!toolName) {
    // -M03: WS handler 层 console 残留
    _debugLogger.warn(
      '[TOOL_START] tool_name 缺失，跳过该工具调用: msgId=%s pipelineId=%s',
      messageId?.slice(0, 12), pipelineId?.slice(0, 8),
    )
    return
  }
  _debugLogger.debug(
    `[TOOL_START] tool=%s callId=%s pipelineId=%s msgId=%s`,
    toolName, callId, pipelineId?.slice(0, 8), messageId?.slice(0, 12),
  )

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  // 去重：检查 parts[] 中是否已存在相同 call_id 的 tool_call part
  const parts: any[] = msg.parts || []
  const existingToolParts = parts.filter((p: any) => p.type === 'tool_call')
  if (parts.some((p: any) => p.type === 'tool_call' && p.callId === callId)) {
    _debugLogger.debug('[TOOL_DEDUP] SKIPPED duplicate: tool=%s callId=%s', toolName, callId?.slice(0, 12))
    return
  }

  // 关闭当前 streaming text part，确保后续文本创建新的 text part
  // 流式阶段文本和工具卡片按 sequence 交错渲染。
  // 如果不关闭当前 text part，后续 stream_chunk 仍追加到 tool 卡片前面的 text part，
  // 导致工具调用后的文本拼到工具卡片前面的文本里，渲染顺序错误。
  const streamingIdx = pipelineStore.getState().findStreamingPartIndex(pipelineId, messageId)
  if (streamingIdx >= 0) {
    const streamingPart = msg.parts[streamingIdx]
    if (streamingPart && streamingPart.type === 'text') {
      pipelineStore.getState().updatePart(pipelineId, messageId, streamingIdx, { state: 'done' })
    }
  }

  // 追加 tool_call part
  _debugLogger.debug(
    '[TOOL_CREATE] tool=%s callId=%s msgId=%s totalToolParts=%d',
    toolName, callId?.slice(0, 12), messageId?.slice(0, 12), existingToolParts.length + 1,
  )
  pipelineStore.getState().appendPart(pipelineId, messageId, {
    type: 'tool_call',
    callId,
    name: toolName,
    args: eventData.args || eventData.data?.args || eventData.data?.tool_args || {},
    state: 'calling',
    // part 渲染按数组顺序（= 追加顺序 = 接收顺序），不分配 sequence。
    containerTaskId: eventData.container_task_id || eventData.data?.container_task_id || undefined,
  })
}

/** 处理工具调用结果事件 在 parts[] 中定位对应的 tool_call part 并更新其状态。 */
export function handleToolResult(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    _debugLogger.warn(
      `[TOOL_RESULT] pipeline_id missing, _threadId=%s msgId=%s tool=%s`,
      eventData.data?._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
      eventData.tool_name || eventData.data?.tool_name,
    )
    return
  }
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  if (!callId) return

  // 通过 call_id 精确匹配 parts[] 中的 tool_call part 并更新
  const partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex >= 0) {
    const resultToolName = eventData.tool_name || eventData.data?.tool_name
    const updates: Record<string, unknown> = {
      state: (eventData.success ?? eventData.data?.success ?? true) === false ? 'error' : 'done',
      result: eventData.result ?? eventData.data?.result,
      // 后端在 tool_result 事件携带的结构化完整数据（含 diff 的 added/removed/old_content/new_content），
      // 流式 result 字段为截断字符串仅供预览；result_data 供工具卡片渲染 +/- 徽标与展开 diff。
      resultData: eventData.result_data ?? eventData.data?.result_data,
      error: eventData.error ?? eventData.data?.error,
      durationMs: eventData.duration_ms ?? eventData.data?.duration_ms,
    }
    // 当 part 的 name 仍为 fallback "unknown" 且 result 事件携带有效 tool_name 时，回填更新
    if (resultToolName && resultToolName !== 'unknown') {
      const msgs = pipelineStore.getState().getMessages(pipelineId)
      const msg = msgs.find((m: any) => m.id === messageId)
      if (msg?.parts?.[partIndex]) {
        const currentPart = msg.parts[partIndex] as any
        if (currentPart.name === 'unknown' || !currentPart.name) {
          updates.name = resultToolName
        }
      }
    }
    pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, updates)
  }
}
