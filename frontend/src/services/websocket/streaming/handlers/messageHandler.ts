/** 新消息事件处理器 后端在 new_message 中携带完整 parts[] 作为权威版本， */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, mergeStreamingParts, terminatePipeline } from './utils'

/** 处理新消息事件 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    terminatePipeline(pipelineId, threadId)
  } else if (threadId) {
    pipelineStore.getState().stopStreaming(threadId)
  }

  // 兜底：new_message 是"本轮完成"的权威信号。若事件的 pipeline_id 字段缺失/
  // 没解析出（resolvePipelineId 返回 null），上面只按 threadId 清理，会漏掉真正
  // 在 streaming 的 pipeline（pipeline_id ≠ thread_id）→ streamingState 残留 →
  // UI 永久卡在"思考中"。这里按 threadId 反查 pipelineSessionMap，把该会话下所有
  // 仍在 streaming 的 pipeline 一并清掉，保证轮次结束后 UI 一定解除生成态。
  if (threadId) {
    const ps = pipelineStore.getState()
    const psm = (ps as any).pipelineSessionMap || {}
    const streaming = (ps as any).streamingState || {}
    for (const [pid, sid] of Object.entries(psm)) {
      if (sid === threadId && streaming[pid]?.isStreaming) {
        ps.stopStreaming(pid)
      }
    }
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

  // 后端发送完整 parts[] 时合并而非覆盖，本地有实质内容就优先保留（详见 mergeStreamingParts）。
  if (serverParts && Array.isArray(serverParts)) {
    const localParts = existingMsg.parts || []
    const { parts: finalParts, content } = mergeStreamingParts(
      localParts, serverParts, data?.content, existingMsg.content,
    )

    if (!content && !finalParts.length) {
      loggers.websocket.warn(
        '[MSG_READY] content 和 parts 均为空，消息将无内容: msgId=%s pipelineId=%s',
        messageId?.slice(0, 12), pipelineId?.slice(0, 12),
      )
    }
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      content,
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
