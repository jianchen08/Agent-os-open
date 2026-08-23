/** 新消息事件处理器 后端在 new_message 中携带完整消息形态（data.message，与 DB 加载
 *  同构），前端经共享 mapper（mapBackendMessageToMessage）生成 parts——流式事件与
 *  历史加载冷热同构，不再依赖 [{type:'text'}] 硬编码形态。
 *
 *  ADR 2026-08-22「认领替代驱逐」：new_message 同时携带 user_message 权威回传
 *  （user 权威 record：id/sequence/content，引擎落库后提取）——前端据此按
 *  client_message_id 认领乐观 user 消息（recordId 双字段范式，UI 寻址 id 永不变），
 *  **不再驱逐**（旧驱逐路径 = 发送后用户消息消失的症状根因）。 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { mapBackendMessageToMessage } from '@/services/api/session'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, mergeStreamingParts, stopPipelineStreaming } from './utils'

/** 处理新消息事件 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  // ADR 2026-08-21「清别人状态」废除：缺 pipeline_id 的事件无法定位归属管道——
  // 记 error 等重连对账，绝不按 threadId 反查清全会话 streaming、不拿 threadId
  // 顶替管道 ID（误杀其他管道流式 = 别人的气泡被掐断）。
  if (!pipelineId) {
    loggers.websocket.error(
      '[NEW_MESSAGE] pipeline_id 缺失，跳过事件（不反查不清别人，等对账补正）: threadId=%s',
      extractThreadId(eventData)?.slice(0, 12) || 'null',
    )
    return
  }

  // 只清事件明确归属的管道
  stopPipelineStreaming(pipelineId)

  const data = eventData?.data || eventData

  // ── 认领（echo）而非驱逐（ADR 2026-08-22）──
  // ① 按 client_message_id 认领乐观 user 消息：权威 record_id/sequence 记入
  //    独立 recordId 字段（UI 寻址 id 保持前端 uuid 不变），status='completed'。
  //    候选缺失（pending 已撤下/刷新后确认到达）→ 以 cmid 为 id 补插权威 user 版
  //    ——「发送后用户消息消失」结构性不可能（症状①③回归锚）。
  // ② 后端 user_message 缺省（旧内核）→ 回落旧驱逐语义（仅撤 pending，
  //    权威 user 由对账补回——兼容旧后端不崩）。
  const confirmedCmid = data?.client_message_id
  const userRecord = data?.user_message as {
    id?: string
    content?: unknown
    sequence?: number
    metadata?: Record<string, unknown> | null
  } | undefined
  if (confirmedCmid) {
    if (userRecord?.id) {
      const result = pipelineStore.getState().claimUserMessage(pipelineId, confirmedCmid, userRecord)
      loggers.websocket.debug(
        '[NEW_MESSAGE] 认领 user 消息: pipeline=%s cmid=%s result=%s',
        pipelineId.slice(0, 12), confirmedCmid.slice(0, 12), result,
      )
    } else {
      // 旧内核兜底：主数组乐观 user 标记 completed（后端已持久化；
      // 权威 id/seq 由对账补正——不驱逐不删除）
      pipelineStore.getState().confirmUserMessage(pipelineId, confirmedCmid)
    }
  }

  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || data?.id
  if (!messageId) return

  const serverMessage = data?.message
  const serverParts = data?.parts
  const backendSeq = data?.sequence ?? eventData?.sequence

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = existingMsgs.find((m: any) => m.id === messageId)

  // 消息不存在 → 忽略（占位消息由 stream_start 创建，不应到达此处）
  if (!existingMsg) return

  // A2：完整消息形态（DB 同构）——经共享 mapper 生成 parts，与历史加载同一套逻辑。
  // preferServer：data.message 是落库权威完整形态（含全部轮次 thinking/text/
  // tool_call），以 server 为基底、本地只补充增量——绝不丢弃 server 权威文本
  // （否则「工具卡片在、最终文本消失」：本地有 tool_call 即视为有内容）。
  if (serverMessage && typeof serverMessage === 'object') {
    const mapped = mapBackendMessageToMessage(serverMessage, threadId || '')
    const localParts = existingMsg.parts || []
    const { parts: finalParts, content } = mergeStreamingParts(
      localParts, mapped.parts, mapped.content, existingMsg.content,
      { preferServer: true },
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

  // 旧形态兜底：后端发送 parts[] 时合并而非覆盖，本地有实质内容就优先保留（详见 mergeStreamingParts）。
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
