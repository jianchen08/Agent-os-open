/** 新消息事件处理器 后端在 new_message 中携带完整消息形态（data.message，与 DB 加载
 *  同构），前端经共享 mapper（mapBackendMessageToMessage）生成 parts——流式事件与
 *  历史加载冷热同构，不再依赖 [{type:'text'}] 硬编码形态。
 *
 *  「认领替代驱逐」（[来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]）：
 *  new_message 同时携带 user_message 权威回传
 *  （user 权威 record：id/sequence/content，引擎落库后提取）——前端据此按
 *  client_message_id 认领乐观 user 消息（recordId 双字段范式，UI 寻址 id 永不变），
 *  **不再驱逐**（旧驱逐路径 = 发送后用户消息消失的症状根因）。 */
import { extractNotifyText, notifyPipelineMessage } from '@/lib/systemNotifier'
import { mapBackendMessageToMessage } from '@/services/api/session'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'
import { resolvePipelineId } from '../router'
import { extractMessageId, extractThreadId, mergeStreamingParts } from './utils'

/** 处理新消息事件 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  // 「清别人状态」已废除：缺 pipeline_id 的事件无法定位归属管道——
  // 记 error 等重连对账，绝不按 threadId 反查清全会话 streaming、不拿 threadId
  // 顶替管道 ID（误杀其他管道流式 = 别人的气泡被掐断）。
  if (!pipelineId) {
    loggers.websocket.error(
      '[NEW_MESSAGE] pipeline_id 缺失，跳过事件（不反查不清别人，等对账补正）: threadId=%s',
      extractThreadId(eventData)?.slice(0, 12) || 'null',
    )
    return
  }

  // 不在此处 stopPipelineStreaming：new_message 是轮级事件（一轮 = 一条消息，
  // 多轮 run 逐轮发射），生成态的唯一终止信号是 run 级
  // pipeline_round_finished / stream_error（streamHandler.handleStreamEnd 同款
  // 约定）。轮级清流式会让 new_message 与 round_finished 的收尾窗口
  // （post 阶段：通知判定/持久化）内 isStreaming 提前翻 false——忙时发送
  // 误走非忙分支（乐观气泡与待发队列同屏互斥破坏）、轮间生成态闪烁。

  const data = eventData?.data || eventData

  claimOrInjectUserMessage(pipelineId, data)

  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || data?.id
  if (!messageId) return

  // 无完整形态时走 parts[] 兜底合并；再无则仅由尾部同步 sequence
  if (!applyServerPayload(pipelineId, threadId, messageId, eventData, data)) {
    applyLegacyPartsMessage(pipelineId, messageId, data, eventData)
  }

  notifyLandedAssistantMessage(pipelineId, threadId, messageId, data)

  // fallback: 仅更新 sequence
  const backendSeq = data?.sequence ?? eventData?.sequence
  if (backendSeq != null) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      sequence: backendSeq,
    } as any)
  }
}

/**
 * 消息落定后的系统级通知（壳能力接线：Electron toast / macOS 通知中心 / libnotify）。
 *
 * 弹出唯一依据 = 后端判定器写入消息 metadata 的 notify_worthy 标记（键名契约）：
 * 标记 → 无视可见性/焦点必弹；无标记 → 一律静默（前台后台都不弹——防 away 时
 * 任务管道消息轰炸；触发器/注入轮是否弹出也由判定器打标接管，前端不再按
 * proactive/可见性兜底）。
 *
 * 标记从 new_message 权威完整形态（data.message，DB 同构）读取——与
 * claimOrInjectUserMessage 读 userRecord.metadata 同一现役模式；流式占位经
 * updateMessage 只补内容面（metadata 不落 store），事件对象才是标记的到达面。
 * threadId（事件 _threadId）随通知透传：点击通知跳转对应对话。
 * 通知失败静默降级，不阻塞消息流（notifyPipelineMessage 永不 reject）。
 */
function notifyLandedAssistantMessage(
  pipelineId: string,
  threadId: string | undefined,
  messageId: string,
  data: Record<string, any> | undefined,
): void {
  const landed = pipelineStore
    .getState()
    .getMessages(pipelineId)
    .find((m) => m.id === messageId)
  if (!landed || landed.role !== 'assistant') return

  const notifyWorthy = data?.message?.metadata?.notify_worthy === true

  void notifyPipelineMessage({
    notifyWorthy,
    title: '灵汐',
    body: extractNotifyText(landed),
    threadId,
    dedupeKey: messageId,
  })
}

/**
 * 认领（echo）/注入 user 权威回传：
 * ① 按 client_message_id 认领乐观 user 消息：权威 record_id/sequence 记入
 *    独立 recordId 字段（UI 寻址 id 保持前端 uuid 不变），status='completed'。
 *    候选缺失（pending 已撤下/刷新后确认到达）→ 以 cmid 为 id 补插权威 user 版
 *    ——「发送后用户消息消失」结构性不可能（症状①③回归锚）。
 * ② 后端 user_message 缺省（旧内核）→ 回落旧驱逐语义（仅撤 pending，
 *    权威 user 由对账补回——兼容旧后端不崩）。
 * ③ 无 cmid 的注入消息（触发器/任务/HTTP，ADR-2026-08-26）：user 权威回传
 *    补插为 user 气泡——用户可见"给管道发了什么"。按 id 幂等；顺序按 sequence 落位。
 */
function claimOrInjectUserMessage(
  pipelineId: string,
  data: Record<string, any> | undefined,
): void {
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
      // 事件缺权威回传（版本错配窗口）：不做无 key 本地终态化——无 seq 终态
      // 消息按排序键永久垫底；气泡留待对账按 cmid 收敛（幂等键随 blob 持久化）。
      loggers.websocket.warn(
        '[NEW_MESSAGE] cmid 认领缺权威 user_record（内核/前端版本错配？），留待对账: pipeline=%s cmid=%s',
        pipelineId.slice(0, 12), confirmedCmid.slice(0, 12),
      )
    }
  } else if (userRecord?.id) {
    // 无 cmid 的注入消息（触发器/任务/HTTP，ADR-2026-08-26）：user 权威回传
    // 补插为 user 气泡——用户可见"给管道发了什么"。按 id 幂等（重复事件不双插）；
    // 顺序按 sequence 落位（触发器消息出现在 assistant 回复之前，与后端消息序一致）。
    pipelineStore.getState().ensureInjectedUserMessage(pipelineId, userRecord as never)
  }
}

/** 消息内容落地：服务端完整形态（A2 冷热同构）合并。命中并应用返回 true */
function applyServerPayload(
  pipelineId: string,
  threadId: string | undefined,
  messageId: string,
  eventData: any,
  data: Record<string, any> | undefined,
): boolean {
  const serverMessage = data?.message
  const backendSeq = data?.sequence ?? eventData?.sequence

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = existingMsgs.find((m: any) => m.id === messageId)

  // 消息不存在 → 忽略（占位消息由 stream_start 创建，不应到达此处）
  if (!existingMsg || !(serverMessage && typeof serverMessage === 'object')) return false

  // A2：完整消息形态（DB 同构）——经共享 mapper 生成 parts，与历史加载同一套逻辑。
  // preferServer：data.message 是落库权威完整形态（含全部轮次 thinking/text/
  // tool_call），以 server 为基底、本地只补充增量——绝不丢弃 server 权威文本
  // （否则「工具卡片在、最终文本消失」：本地有 tool_call 即视为有内容）。
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
    // 服务端权威 status（new_message 从消息 blob 读取：中断 interrupted /
    // 错误 error），mapper 缺省 completed——不再无条件覆盖为 completed。
    status: mapped.status || 'completed',
    sequence: backendSeq ?? existingMsg.sequence,
  } as any)
  return true
}

/** 旧形态兜底：后端发送 parts[] 时合并而非覆盖，本地有实质内容就优先保留（详见 mergeStreamingParts）。 */
function applyLegacyPartsMessage(
  pipelineId: string,
  messageId: string,
  data: Record<string, any> | undefined,
  eventData: any,
): void {
  const serverParts = data?.parts
  const backendSeq = data?.sequence ?? eventData?.sequence
  if (!(serverParts && Array.isArray(serverParts))) return

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = existingMsgs.find((m: any) => m.id === messageId)
  if (!existingMsg) return

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
}
