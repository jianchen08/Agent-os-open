/**
 * 新消息事件处理器
 *
 * 后端在 new_message 中携带完整 parts[] 作为权威版本，
 * 前端用其完整替换流式过程中增量构建的消息。
 * 若消息不存在（后端注入的 trigger/system 消息），自动创建。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline } from './utils'

/**
 * 处理新消息事件
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    terminatePipeline(pipelineId, threadId)
  } else if (threadId) {
    pipelineStore.getState().stopStreaming(threadId)
  }

  if (!pipelineId) return

  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  if (!messageId) return

  const data = eventData?.data || eventData
  const serverParts = data?.parts
  const backendSeq = data?.sequence ?? eventData?.sequence

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = existingMsgs.find((m: any) => m.id === messageId)

  // 消息不存在 → 忽略（占位消息由 stream_start 创建，不应到达此处）
  if (!existingMsg) return

  // 后端发送了完整 parts[] → 合并而非覆盖
  // BUG-FIX-fix_20260617_new_msg_overwrite:
  // 问题根因: 后端 new_message 的 parts 只包含最后一轮 iteration 的内容（state.raw_thinking
  //   /raw_tool_calls/raw_result 每轮被覆盖）。用 serverParts 完整替换会导致流式过程中
  //   增量构建的前几轮 thinking/text/tool 内容全部丢失，用户只看到最后一轮的 AI 回复。
  // 修复方案: 合并本地已有 parts 和 server parts。本地 parts 保留（流式增量内容），
  //   server parts 补充缺失的部分。如果 server parts 有内容则用它（后端权威），
  //   否则保留本地。
  if (serverParts && Array.isArray(serverParts)) {
    const updatedContent = data?.content != null ? data.content : existingMsg.content

    // 合并策略：server parts 有实质内容时用 server（权威），否则保留本地流式 parts
    const localParts = existingMsg.parts || []
    const serverHasContent = serverParts.length > 0 && serverParts.some(
      (p: any) => (p.type === 'text' && p.content) || (p.type === 'thinking' && p.content) || (p.type === 'tool_call')
    )
    const finalParts = serverHasContent
      ? (localParts.length > serverParts.length ? localParts : serverParts)
      : localParts

    if (!updatedContent && !finalParts.length) {
      loggers.websocket.warn(
        '[MSG_READY] content 和 parts 均为空，消息将无内容: msgId=%s pipelineId=%s',
        messageId?.slice(0, 12), pipelineId?.slice(0, 12),
      )
    }
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      content: updatedContent || existingMsg.content,
      parts: finalParts.length > 0 ? finalParts : undefined,
      status: 'completed',
      sequence: backendSeq ?? existingMsg.sequence,
    } as any)
    return
  }

  // fallback: 仅更新 sequence
  if (backendSeq != null) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      sequence: backendSeq,
    } as any)
  }
}
