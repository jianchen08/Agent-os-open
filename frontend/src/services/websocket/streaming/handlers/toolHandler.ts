/**
 * 工具调用事件处理器（start / result）
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resetChunkTimeout } from '../chunkTimeout'
import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/**
 * 处理工具调用开始事件
 *
 * FIX: toolCalls 去重改为只看 call_id 是否已存在（不看状态），避免 WS 乱序时重复。
 */
export function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  _debugLogger.debug(
    `[TOOL_START] tool=%s callId=%s pipelineId=%s msgId=%s`,
    toolName, callId || '(no-call-id)', pipelineId?.slice(0, 8), messageId?.slice(0, 12),
  )

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const existingCalls: any[] = msg.toolCalls || []
  const existingBlocks: any[] = msg.contentBlocks || []

  if (callId) {
    if (existingCalls.some((tc: any) => tc.call_id === callId)) return
    if (existingBlocks.some((b: any) => b.type === 'tool_call' && b.toolCall?.call_id === callId)) return
  } else {
    const runningCount = existingCalls.filter(
      (tc: any) => tc.tool_name === toolName && tc.status === 'running',
    ).length
    if (runningCount > 0) return
    const blockRunningCount = existingBlocks.filter(
      (b: any) => b.type === 'tool_call' && b.toolCall?.tool_name === toolName && b.toolCall?.status === 'running',
    ).length
    if (blockRunningCount > 0) return
  }

  const finalCallId = callId || `call_${toolName}_${Date.now()}`
  // BUG-FIX-fix_20260522_tool_order: 提取后端发送的 sequence 用于排序
  const sequence = eventData.sequence ?? eventData.data?.sequence
  const newToolCall = {
    call_id: finalCallId, tool_name: toolName,
    tool_args: eventData.args || eventData.data?.args || {},
    status: 'running' as const, started_at: new Date().toISOString(),
    sequence,
  }
  const toolBlock = { type: 'tool_call' as const, toolCall: newToolCall, sourceId: messageId, sequence }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    toolCalls: [...existingCalls, newToolCall],
    contentBlocks: [...existingBlocks, toolBlock],
  } as any)
}

/**
 * 处理工具调用结果事件
 *
 * FIX: 优先用 call_id 精确匹配，无 callId 时 fallback 到 tool_name 匹配。
 */
export function handleToolResult(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // FIX: pipeline_id 缺失时记录 warn
    _debugLogger.warn(
      `[TOOL_RESULT] pipeline_id missing, _threadId=%s msgId=%s tool=%s`,
      eventData.data?._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
      eventData.tool_name || eventData.data?.tool_name,
    )
    return
  }
  const messageId = extractMessageId(eventData)
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const callId = eventData.call_id || eventData.data?.call_id

  const buildUpdated = (existing: any[]) => {
    let matched = false
    const updated = existing.map((tc) => {
      if (callId && tc.call_id === callId) {
        matched = true
        return {
          ...tc,
          status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
          result: eventData.result ?? eventData.data?.result,
          completed_at: new Date().toISOString(),
          duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
        }
      }
      if (!callId && !matched && tc.tool_name === toolName && tc.status === 'running') {
        matched = true
        return {
          ...tc,
          status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
          result: eventData.result ?? eventData.data?.result,
          completed_at: new Date().toISOString(),
          duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
        }
      }
      return tc
    })
    if (!matched) {
      updated.push({
        call_id: callId || `call_${Date.now()}`, tool_name: toolName, tool_args: {},
        status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
        result: eventData.result ?? eventData.data?.result, completed_at: new Date().toISOString(),
        duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
      })
    }
    return updated
  }

  const patchBlocks = (prevBlocks: any[], updated: any[]) => {
    const blocks = prevBlocks ? [...prevBlocks] : []
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i]
      if (b.type === 'tool_call' && b.toolCall?.status === 'running') {
        const match = updated.find((tc) => tc.call_id === b.toolCall!.call_id && tc.status !== 'running')
        if (match) blocks[i] = { ...b, toolCall: match }
      }
    }
    return blocks
  }

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const updated = buildUpdated(msg.toolCalls || [])
  const blocks = patchBlocks(msg.contentBlocks, updated)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    toolCalls: updated,
    contentBlocks: blocks,
  } as any)
}
