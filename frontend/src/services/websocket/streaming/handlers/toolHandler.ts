/** 工具调用事件处理器（start / result） 仅使用 parts[] 统一路径，已移除旧 toolCalls / contentBlocks 兼容代码。 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { flushStreamChunkBuffer } from './streamHandler'
import { ensureStreamingPlaceholder, extractMessageId, extractThreadId } from './utils'

const _debugLogger = loggers.websocket

/** 处理工具调用开始事件 向 parts[] 追加一个 tool_call part；若 call_id 缺失则跳过（等数据完整再渲染）。 */
export function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 先冲刷缓冲的 chunk：tool_start 是 part 结构边界变更点（与 thinking_end / stream_end 同类），
  // 必须在变更前把 RAF 缓冲里的正文 chunk 落到当前 text part。否则本函数会把当前
  // streaming text part 置为 done，随后 RAF flush 时 findStreamingPartIndex 找不到
  // streaming text part，会新建 text part 追加到 tool_call 之后 → 正文被劈到工具后面，
  // 多轮交错时表现为「思考-工具-文本-文本-思考」错乱。
  flushStreamChunkBuffer()

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
  if (!msg) {
    // tool_start 到达时消息占位可能不存在（stream_start 断线丢失/乱序/合并改名未命中），
    // 自动创建占位符——对齐 handleStreamChunk / handleThinkingStart 的
    // "有消息就有占位符" 语义：占位缺失时自动补建，任何 tool 事件自愈。
    _debugLogger.warn(
      `[TOOL_START] msg not found, auto-creating placeholder: pipeline=%s msgId=%s tool=%s`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), toolName,
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }

  // 去重：检查 parts[] 中是否已存在相同 call_id 的 tool_call part
  const parts: any[] = msg?.parts || []
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
    const streamingPart = msg?.parts?.[streamingIdx]
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

/** 处理工具执行中进度事件（task_observability 任务 2）。

 * bash 等长任务执行中经 frontend.emit 推 tool_progress（stdout 增量，
 * 源头已按 1KB/2s 节流）：按 call_id 定位 tool_call part，追加 partialOutput
 * 供 ActivityCard「执行输出」实时渲染，并更新 currentStep 运行时摘要。
 */
/** partialOutput 累计字符上限（保留尾部，防超大输出把 store 撑爆） */
const PARTIAL_OUTPUT_CAP = 64 * 1024

export function handleToolProgress(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return
  const callId = eventData.call_id || eventData.data?.call_id
  if (!callId) return

  const data = eventData.data || eventData
  const delta: string = typeof data?.delta === 'string' ? data.delta : ''
  if (!delta) return

  const partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex < 0) {
    // tool_start 未达/丢失：进度无法定位，静默丢弃（结果仍会由 tool_result 补齐）
    _debugLogger.debug(
      '[TOOL_PROGRESS] tool_call part not found, skip: tool=%s callId=%s',
      data?.tool_name, callId?.slice(0, 12),
    )
    return
  }

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  const part = msg?.parts?.[partIndex] as any
  if (!part) return
  // 迟到进度（part 已 done/error）：结果已定，忽略避免覆盖
  if (part.state === 'done' || part.state === 'error' || part.state === 'cancelled') return

  // 追加 delta（尾部截断：最新输出最有信息量）
  const chunks: string[] = [...(part.partialOutput ?? []), delta]
  const merged = chunks.join('')
  const nextOutput: string[] =
    merged.length > PARTIAL_OUTPUT_CAP ? [merged.slice(merged.length - PARTIAL_OUTPUT_CAP)] : chunks

  // 运行时摘要：已输出 X KB / Ys（「工具跑到哪了」一句话可见）
  const kb = (Number(data?.bytes_read) || 0) / 1024
  const secs = (Number(data?.elapsed_ms) || 0) / 1000
  const currentStep = `已输出 ${kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb.toFixed(1)} KB`} / ${secs.toFixed(1)}s`

  pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, {
    partialOutput: nextOutput,
    currentStep,
  } as any)
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
  let partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex < 0) {
    // tool_result 到达时对应的 tool_call part 可能不存在（tool_start 事件丢失/乱序），
    // 补建 tool_call part 再写入结果，对齐 Python bridge_events.py 的 FIXUP 自动补发
    // tool_start 逻辑（Rust 内核 tool_core 无 FIXUP，前端兜底）——占位缺失时自动补建，
    // 任何 tool 事件自愈。
    const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
    _debugLogger.warn(
      '[TOOL_RESULT] tool_call part not found, FIXUP creating: tool=%s callId=%s msgId=%s',
      toolName, callId?.slice(0, 12), messageId?.slice(0, 12),
    )
    pipelineStore.getState().appendPart(pipelineId, messageId, {
      type: 'tool_call',
      callId,
      name: toolName,
      args: {},
      state: 'calling',
    })
    partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  }

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
