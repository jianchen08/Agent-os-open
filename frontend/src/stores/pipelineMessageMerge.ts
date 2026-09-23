/**
 * 管道消息纯函数助手族（自 pipelineMessageStore 抽出——冻结巨型文件只许缩小
 * 不许增长）。排序/合并/去重/游标/占位重建均为无状态变换：不引 zustand、
 * 不持有 store 状态，store 的 set() 回调内调用。
 */
import { compareMessages } from '@/utils/messageOrder'
import type { TransientStateEntry } from '@/services/api/session'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

/** 单个管道在「内存」中保留的最大消息条数 与 PERSIST_MAX_MESSAGES_PER_PIPELINE（仅持久化裁剪）不同：内存里的 */
const MAX_MESSAGES_PER_PIPELINE_IN_MEMORY = 2000

/** 限制单管道内存消息数，防止无限增长导致浏览器 OOM。 仅在超量时裁剪：按 sequence 排序后保留最新的 N 条。未超限时只做一次 */
export function capMessagesForMemory(msgs: Message[]): Message[] {
  if (msgs.length <= MAX_MESSAGES_PER_PIPELINE_IN_MEMORY) return msgs
  return [...msgs].sort(compareMessages).slice(-MAX_MESSAGES_PER_PIPELINE_IN_MEMORY)
}

/** Parts 统一修改骨架（appendPart/updatePart/appendToPart 共用）：定位消息 → 变换 parts → 提交。
 * 管道/消息不存在或 partIndex 越界时原样返回 state（幂等）；transform 返回 null 表示放弃变更。 */
export function mutateMessageParts<T extends { messagesByPipeline: Record<string, Message[]> }>(
  state: T,
  pipelineId: string,
  messageId: string,
  partIndex: number | null,
  transform: (parts: MessagePart[]) => MessagePart[] | null,
): T {
  const pipelineMessages = state.messagesByPipeline[pipelineId]
  if (!pipelineMessages) return state
  const msgIndex = pipelineMessages.findIndex((m) => m.id === messageId)
  if (msgIndex < 0) return state
  const msg = pipelineMessages[msgIndex]
  if (partIndex !== null && (partIndex < 0 || partIndex >= (msg.parts || []).length)) return state
  const updatedParts = transform(msg.parts || [])
  if (!updatedParts) return state
  const updatedMessages = [...pipelineMessages]
  updatedMessages[msgIndex] = { ...msg, parts: updatedParts, _lastUpdated: Date.now() }
  return {
    ...state,
    messagesByPipeline: { ...state.messagesByPipeline, [pipelineId]: updatedMessages },
  }
}

/**
 * 过滤完全空白的 assistant 消息（无 content、无 parts、无 tool_call part、无 thinking、非 streaming）。
 * 这些消息来自后端记录但不包含可渲染内容，渲染为空气泡。
 *
 * 注意：assistant 可能 content 为空但有 tool_call part（发起工具调用）或 thinking
 * （纯思考）。子任务管道大量存在这种消息，若只检查 content/parts 会误删，
 * 导致消息丢失。必须检查 tool_call part 和 thinking。
 */
export function filterBlankMessages(messages: Message[]): Message[] {
  return messages.filter((m) => {
    if (m.role !== 'assistant') return true
    if (m.status === 'streaming') return true
    const hasContent = m.content && m.content.trim()
    const hasParts = m.parts && m.parts.length > 0
    const hasToolCalls = (m.parts ?? []).some((p) => p.type === 'tool_call')
    const hasThinking = m.thinking && (m.thinking.content || '').trim()
    return hasContent || hasParts || hasToolCalls || hasThinking
  })
}

/** 排序键优先级：sequence → timestamp → id（确保 sequence/timestamp 相同时排序稳定）。
 *  实现与渲染层共用（@/utils/messageOrder），单一真值源。 */
export { compareMessages }

/** 合并两个已排序数组，返回新的已排序数组 */
export function mergeSorted(a: Message[], b: Message[]): Message[] {
  const result: Message[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (compareMessages(a[i], b[j]) <= 0) {
      result.push(a[i++])
    } else {
      result.push(b[j++])
    }
  }
  while (i < a.length) result.push(a[i++])
  while (j < b.length) result.push(b[j++])
  return result
}

/**
 * 判断本地消息是否被 API 权威消息覆盖（即二者是同一条逻辑消息）。
 *
 * 去重规则唯一真相源，全量对账（initFromAPI）与增量补漏（append/prepend）共用：
 * - id 相同 → 同一条（后端 record_id == WS message_id，正常路径）
 * - clientMessageId 相同 → 同一条（user 乐观版 id=前端 UUID，API 版 id=后端
 *   record_id，id 不同但后端从乐观消息回传了相同 clientMessageId）
 * - recordId 相同 → 同一条（双字段范式：本地已认领的 user 消息
 *   UI id 是前端 uuid、API 权威版 id 是后端 record_id，按 recordId 收敛）
 *
 * 命中时本地版让位 API 版（丢弃本地、保留 API），保证全量与增量两条路径的
 * 渲染终态一致 —— 切会话回来（增量）与刷新（全量）不会产生不同的消息列表。
 * （[来源: docs/decisions/2026-08-21-message-idempotency-contract.md] /
 *  [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]：
 *  仅精确键裁决，不做 role::seq 指纹等模糊匹配——
 * 非唯一键，撞号时写错目标/误删，业界流式系统一律事件携带权威 ID 精确匹配。）
 */
export function isCoveredByApi(
  m: Message,
  apiIds: Set<string>,
  apiByClientId: Map<string, Message>,
  apiByRecordId?: Map<string, Message>,
): boolean {
  if (apiIds.has(m.id)) return true
  if (m.clientMessageId && apiByClientId.has(m.clientMessageId)) return true
  // [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md] 双字段范式：
  // 本地已认领 user（UI id=uuid, recordId=mc_ 指纹）
  // 与 API 权威版（id=后端 record_id，与 recordId 同值）收敛——按 recordId 命中
  // API id 集即同一条，刷新/补漏后不产生重复气泡
  if (m.recordId && apiIds.has(m.recordId)) return true
  if (m.recordId && apiByRecordId?.has(m.recordId)) return true
  return false
}

/**
 * 增量补漏（append/prepend）合并：API 仅返回新增消息（after_sequence 增量或
 * before_sequence 翻页），本地已有历史必须全部保留 —— 与 initFromAPI 的全量对账
 * 不同（全量会丢弃 API 没返回的旧消息，增量必须保留 ≤ bottomCursor 的历史）。
 *
 * 去重规则与全量路径共用 isCoveredByApi：本地消息若被 API 覆盖（同 id 或同
 * clientMessageId），让位 API 版，保证增量与全量渲染终态一致。user 乐观版
 * （id=前端 UUID）与 API 版（id=后端 record_id）同 clientMessageId 时不会并存。
 *
 * 仅做合并 + 去重，后处理（流式合并 / 空气泡过滤 / 内存封顶）由调用方按需追加。
 */
export function mergeIncrementalApiWithLocal(apiSorted: Message[], existing: Message[]): Message[] {
  if (apiSorted.length === 0) return existing
  if (existing.length === 0) return apiSorted

  const apiIds = new Set(apiSorted.map((m) => m.id))
  const apiByClientId = new Map<string, Message>()
  const apiByRecordId = new Map<string, Message>()
  for (const m of apiSorted) {
    if (m.clientMessageId) apiByClientId.set(m.clientMessageId, m)
    if (m.recordId) apiByRecordId.set(m.recordId, m)
  }

  // 本地消息：被 API 覆盖 → 让位 API 版（丢弃本地乐观版）；其余全部保留（增量语义）。
  const keptLocal = existing.filter((m) => !isCoveredByApi(m, apiIds, apiByClientId, apiByRecordId))

  // mergeSorted 要求两边各自升序；keptLocal 来自 existing（可能无序），先排序。
  return mergeSorted([...keptLocal].sort(compareMessages), apiSorted)
}

/** 计算 bottom 游标（只增不减，防止流式消息 sequence 临时值导致回退） 取 max(API 返回的最大 seq, 现有 bottomCursor)，只增不减。 */
export function calculateBottomCursor(finalMessages: Message[], existingCursor: number | undefined): number {
  const apiBottomCursor = finalMessages.length > 0
    ? finalMessages.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0)
    : 0
  return Math.max(apiBottomCursor, existingCursor ?? 0)
}

/**
 * 从存活中间态快照重建流式占位消息（ADR 2026-08-27 §2.6 前端刷新恢复）。
 *
 * 形状与 ensureStreamingPlaceholder（services/websocket/streaming/handlers/utils.ts）
 * 产出的流式占位同构：id=message_id、role='assistant'、status='streaming'、
 * content=快照 text 块拼接、无 parts、sequence 挂空（权威值由 stream_end
 * final_sequence / new_message 对账纠正）、_lastUpdated 打新鲜戳。后续 chunk
 * 到达时 blockHandler 按 id 命中占位续写（findStreamingPartIndex/appendPart），
 * new_message 合并（preferServer）以 server 权威为基底、本地 content 只作
 * 兜底——前缀文本不会重复。
 *
 * 快照只携带 text 块内容（transient.rs accumulate_chunk：reasoning 仅长度不
 * 携带文本），故 thinking 不重建——思考区由后续 reasoning_delta 重新累积，
 * 与流式占位（无 thinking）同构。非 chunk: 前缀的中间态键（progress 等）不
 * 属于消息占位，返回 null 跳过。
 */
export function buildTransientPlaceholder(
  entry: TransientStateEntry,
  sessionId: string,
): Message | null {
  const CHUNK_PREFIX = 'chunk:'
  if (!entry?.key?.startsWith(CHUNK_PREFIX)) return null
  const messageId = entry.key.slice(CHUNK_PREFIX.length)
  if (!messageId) return null
  const blocks = Array.isArray(entry.value?.blocks) ? entry.value.blocks : []
  const content = blocks
    .filter((b) => b?.type === 'text' && typeof b.content === 'string')
    .map((b) => b.content as string)
    .join('')
  return {
    id: messageId,
    sessionId,
    role: 'assistant',
    content,
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'streaming',
    _lastUpdated: Date.now(),
  } as Message
}
