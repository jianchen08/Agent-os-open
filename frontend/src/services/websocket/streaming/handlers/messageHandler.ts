/**
 * 新消息事件处理器
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline } from './utils'

/**
 * 从 API 事件数据中为消息构建 parts[] 数组
 *
 * 当消息尚无 parts 时，根据 content / thinking / toolCalls 构建。
 *
 * @param content - 消息文本内容
 * @param thinking - 思考块数据
 * @param toolCalls - 工具调用列表
 * @returns 构建好的 parts 数组
 */
function buildPartsFromApiData(
  content: string | undefined,
  thinking: any,
  toolCalls: any[] | undefined,
): any[] {
  const parts: any[] = []

  if (thinking?.content && thinking.content.trim()) {
    parts.push({
      type: 'thinking',
      thinking: { content: thinking.content.trim(), isThinking: false },
      state: 'done',
    })
  }

  if (content && content.trim()) {
    parts.push({ type: 'text', text: content.trim(), state: 'done' })
  }

  if (toolCalls && toolCalls.length > 0) {
    for (const tc of toolCalls) {
      parts.push({ type: 'tool_call', toolCall: tc, state: 'done' })
    }
  }

  return parts
}

/**
 * 将消息中所有 streaming 状态的 parts 标记为 done
 *
 * @param msg - 消息对象
 * @returns 更新后的 parts 数组
 */
function finalizeStreamingParts(msg: any): any[] {
  return (msg.parts || []).map((p: any) =>
    p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
  )
}

/**
 * 处理新消息事件
 *
 * 流程：
 * 1. 终止管道并清理流式状态
 * 2. 若消息已有 parts 且有文本内容，仅更新 status 并将 streaming parts 改为 done
 * 3. 若消息无 parts 或无文本内容，从 API 数据构建 parts[]
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    // BUG-FIX-fix_20260522: 标记管道已终止（new_message），防止 ensureStreamingPlaceholder 重新启动
    terminatePipeline(pipelineId, threadId)
    // BUG-FIX-fix_20260522_stream_end_over_cleanup:
    // new_message 同样需要清理所有关联的 streamingTabs
    const currentActivePipelineId = pipelineStore.getState().activePipelineId
    if (currentActivePipelineId && currentActivePipelineId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(currentActivePipelineId, false)
    }
    if (threadId && threadId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(threadId, false)
    }
  } else if (threadId) {
    // FIX: pipeline_id 缺失时仍清理 threadId 的 tab 状态
    pipelineStore.getState().stopStreaming(threadId)
  }

  if (!pipelineId) return

  // messageHandler 还需要兼容更多消息 ID 来源（event.message?.id, eventData.data?.id）
  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  if (!messageId) return

  const finalContent = eventData?.content || eventData?.data?.content || eventData?.data?.final_content
  const data = eventData?.data || eventData

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const existing = msgs.find((m: any) => m.id === messageId)
  if (!existing) return

  // 基于 parts[] 判断消息是否已有文本内容
  const existingParts = (existing as any).parts || []
  const hasTextParts = existingParts.some((p: any) => p.type === 'text' && p.text?.trim())

  if (hasTextParts) {
    // 已有文本 parts，仅更新 status 并将 streaming parts 改为 done
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      parts: finalizeStreamingParts(existing),
    } as any)
  } else {
    // 无文本 parts，从 API 数据构建 parts[]
    const builtParts = buildPartsFromApiData(finalContent, data?.thinking, data?.toolCalls)
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      ...(finalContent ? { content: finalContent } : {}),
      ...(builtParts.length > 0 ? { parts: builtParts } : {}),
    } as any)
  }
}
