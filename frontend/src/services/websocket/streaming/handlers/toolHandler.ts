/**
 * 工具调用事件处理器（start / result）
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'

import { resetChunkTimeout } from '../chunkTimeout'
import { resolvePipelineId } from '../router'

/**
 * 处理工具调用开始事件
 *
 * BUG-FIX-fix_20260513_duplicate_toolcall:
 * 问题根因: 去重条件只检查 status==='running'，当 tool_result 先于 tool_start 到达（WS 乱序）
 *          时，已有的 completed 状态条目不匹配去重条件，导致追加重复的 running 条目。
 *          同时 contentBlocks 的去重也存在同样问题（只检查 call_id 存在，不看状态），
 *          但 call_id 的去重是正确的（不看状态），所以 contentBlocks 不会重复，但 toolCalls 数组会。
 * 修复方案:
 *   1. toolCalls 去重改为只看 call_id 是否已存在（不看状态），与 contentBlocks 保持一致
 *   2. 无 callId 时仍按 tool_name + running 去重
 * 影响范围: WebSocket 消息乱序场景下的 toolCall 数据一致性
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
 *
 * BUG-FIX-fix_20260513_duplicate_toolcall:
 * 问题根因: buildUpdated 的 map 匹配条件是 tool_name + status==='running'，
 *          当同名工具连续调用两次时，map 只更新第一个 running 的，第二个被忽略。
 *          fallback 的判断条件也存在漏洞：用 tool_name 匹配而非 call_id。
 *          当 tool_result 先于 tool_start 到达（WS 乱序），不存在 running 条目，
 *          fallback 会创建新条目，后续 tool_start 又追加一个 running 条目，产生重复。
 * 修复方案:
 *   1. 优先用 call_id 精确匹配（而非 tool_name）
 *   2. 无 callId 时 fallback 到 tool_name 匹配
 *   3. fallback 判断也改为 call_id 维度
 * 影响范围: WebSocket 消息乱序和同名工具连续调用场景
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
