import type { Message } from '@/types/models'

/**
 * regenerate 出站帧 user_message_id 的键空间解析（双字段范式）。
 *
 * 消息 UI 寻址 id 是前端 uuid，后端权威持久化主键在 recordId（mc_ 指纹，
 * user 消息认领时写入，见 pipelineMessageStore isCoveredByApi 注释）；历史
 * 回读消息无 recordId——其 id 本身即后端 record_id。内核 find_target_user_seq
 * 按 message_id（record_id 键空间）精确匹配，出站必须取 recordId ?? id；
 * recordId 缺失（目标尚未认领的本地乐观窗口）时回退 UI id。
 */
export function resolveWireUserMessageId(message: Pick<Message, 'id' | 'recordId'>): string {
  return message.recordId ?? message.id
}
