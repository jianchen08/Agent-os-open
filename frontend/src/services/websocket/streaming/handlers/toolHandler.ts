/**
 * 工具调用事件处理器（start / result）
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'

import { resetChunkTimeout } from '../chunkTimeout'
import { resolvePipelineId } from '../router'

/**
 * 处理工具调用开始事件
 */
export function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  console.warn(
    `%c[TOOL_START] tool=%s callId=%s pipelineId=%s msgId=%s`,
    'color:orange;font-weight:bold',
    toolName, callId || '(no-call-id)', pipelineId?.slice(0, 8), messageId?.slice(0, 12),
  )

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const existingCalls: any[] = msg.toolCalls || []
  const existingBlocks: any[] = msg.contentBlocks || []

  if (callId) {
    if (existingCalls.some((tc: any) => tc.call_id === callId && tc.status === 'running')) return
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
  const newToolCall = {
    call_id: finalCallId, tool_name: toolName,
    tool_args: eventData.args || eventData.data?.args || {},
    status: 'running' as const, started_at: new Date().toISOString(),
  }
  const toolBlock = { type: 'tool_call' as const, toolCall: newToolCall, sourceId: messageId }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    toolCalls: [...existingCalls, newToolCall],
    contentBlocks: [...existingBlocks, toolBlock],
  } as any)
}

/**
 * 处理工具调用结果事件
 */
export function handleToolResult(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const callId = eventData.call_id || eventData.data?.call_id

  const buildUpdated = (existing: any[]) => {
    const updated = existing.map((tc) => {
      if (tc.tool_name === toolName && tc.status === 'running') {
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
    if (!updated.some((tc) => tc.tool_name === toolName && tc.status !== 'running')) {
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
