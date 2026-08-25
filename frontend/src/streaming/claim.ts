/**
 * 流式协议认领（claim）模块 —— [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]。
 *
 * 症状根因①③（用户真机：发送后用户消息消失，刷新恢复）：new_message 事件
 * 到达时前端只做了「驱逐」——把 pending 乐观消息撤下，却没有任何路径把后端
 * 已持久化的 user 权威版补进主列表 → 驱逐即消失。
 *
 * 本模块实现「认领（echo）」：按 client_message_id 找到乐观 user 消息，
 * 权威 record_id/sequence 记入独立 `recordId` 字段，**UI 寻址 id（React key）
 * 保持前端 uuid 不变**（id 迁移会重挂气泡——2b1940b00 修订否决的旧语义），
 * status='completed'。业界双字段范式：Telegram client_msg_id + 权威 message_id
 * 各司其职，对账按 (id ∪ clientMessageId) 双键匹配。
 *
 * 纯函数、零 store 依赖——认领规则可单测，store 适配层在 pipelineMessageStore。
 */

/** 后端 new_message 事件携带的 user 权威 record（认领回传）。 */
export interface UserRecord {
  /** 后端权威 record_id（user 消息 = compute_message_id 指纹 mc_ 前缀） */
  id?: string
  content?: unknown
  /** 权威 seq（排序键；缺失回落 timestamp，兼容旧后端） */
  sequence?: number
  metadata?: Record<string, unknown> | null
}

/** 认领结果：三种处置之一，由调用方（store 层）落数据。 */
export type ClaimAction =
  | { kind: 'upgrade'; messageId: string; recordId: string; sequence?: number }
  | { kind: 'insert'; messageId: string; recordId: string; sequence?: number }
  | { kind: 'skip'; reason: string }

export interface ClaimCandidate {
  /** 消息 UI 寻址 id（乐观 = 前端 uuid；认领后保持不变） */
  id: string
  /** 乐观幂等键（发送瞬间生成，后端落库回传） */
  clientMessageId?: string
  status?: string
  /** 已认领的权威 record_id（幂等判据） */
  recordId?: string
}

/**
 * 认领裁决：给定乐观消息候选（pending 区或主数组），决定如何升级为权威。
 *
 * - 候选已带同 recordId → skip（幂等，重复 new_message / 对账重入不重复升级）
 * - 候选存在 → upgrade（保留 UI id，记入 recordId + 权威 seq）
 * - 候选缺失（刷新后 / pending 已超时撤下 / 断线期间确认到达）→ insert
 *   （权威 user 不能丢——后端已持久化，必须补进主列表）
 */
export function decideClaim(
  candidate: ClaimCandidate | undefined,
  userRecord: UserRecord,
  cmid: string,
): ClaimAction {
  const recordId = userRecord.id
  if (!recordId) return { kind: 'skip', reason: 'user_record 缺权威 id（旧后端未回传）' }
  if (!candidate) {
    return { kind: 'insert', messageId: cmid, recordId, sequence: userRecord.sequence }
  }
  if (candidate.recordId === recordId) {
    return { kind: 'skip', reason: '已认领（同 recordId，幂等）' }
  }
  if (candidate.clientMessageId && candidate.clientMessageId !== cmid) {
    return { kind: 'skip', reason: 'cmid 不匹配（不认领别人的消息）' }
  }
  return { kind: 'upgrade', messageId: candidate.id, recordId, sequence: userRecord.sequence }
}
