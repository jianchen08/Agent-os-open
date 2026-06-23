/**
 * 工具调用事件处理器（start / result）
 * 仅使用 parts[] 统一路径，已移除旧 toolCalls / contentBlocks 兼容代码。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/**
 * 处理工具调用开始事件
 *
 * 向 parts[] 追加一个 tool_call part；若 call_id 缺失则跳过（等数据完整再渲染）。
 *
 * BUG-FIX-fix_20260603_tool_duplicate_cards:
 * 问题根因: 两个层面造成重复。
 *   层面1（toolHandler）: LLM 流式首个 delta 不含 call_id 时，原代码用 Date.now() 生成
 *     fallback callId，去重失效，产生 ghost part。修复: call_id 缺失时跳过。
 *   层面2（ChatContainer）: mergeConsecutiveAssistantMessages 合并多个 assistant
 *     消息时，不同消息里相同 callId 的 tool_call part 会被重复计入。修复: 合并时按 callId 去重。
 * 影响范围: 流式场景下工具卡片重复渲染（1个工具调用出现2~3张卡片）
 * 修复日期: 2026-06-03
 */
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
    // BUG-FIX-M03: WS handler 层 console 残留
    // 问题根因: 数据异常（tool_name 缺失）用 console.warn 且打印整个 eventData。
    // 修复方案: 改用正式 logger.warn，仅打印定位字段，避免泄露内部事件数据。
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

  /* ---- 去重：检查 parts[] 中是否已存在相同 call_id 的 tool_call part ---- */
  const parts: any[] = msg.parts || []
  const existingToolParts = parts.filter((p: any) => p.type === 'tool_call')
  if (parts.some((p: any) => p.type === 'tool_call' && p.callId === callId)) {
    _debugLogger.debug('[TOOL_DEDUP] SKIPPED duplicate: tool=%s callId=%s', toolName, callId?.slice(0, 12))
    return
  }

  /* ---- 关闭当前 streaming text part，确保后续文本创建新的 text part ---- */
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

  /* ---- 追加 tool_call part ---- */
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
    sequence: eventData.sequence ?? eventData.data?.sequence ?? Date.now(),
    containerTaskId: eventData.container_task_id || eventData.data?.container_task_id || undefined,
  })
}

/**
 * 处理工具调用结果事件
 *
 * 在 parts[] 中定位对应的 tool_call part 并更新其状态。
 */
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

  /* ---- 通过 call_id 精确匹配 parts[] 中的 tool_call part 并更新 ---- */
  const partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex >= 0) {
    // BUG-FIX-fix_20260603_tool_unknown_card:
    // 问题根因: LLM 流式返回 tool_calls 时，首个 delta 可能不含 function.name，
    //   后端 tool_start 事件带 "unknown"。切换会话后从历史 API 加载数据完整所以正常。
    // 修复方案: 在 tool_result 事件中回填工具名称，修正 tool_start 阶段的 "unknown"。
    // 影响范围: 流式场景下工具卡片标题显示
    // 修复日期: 2026-06-03
    const resultToolName = eventData.tool_name || eventData.data?.tool_name
    const updates: Record<string, unknown> = {
      state: (eventData.success ?? eventData.data?.success ?? true) === false ? 'error' : 'done',
      result: eventData.result ?? eventData.data?.result,
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
