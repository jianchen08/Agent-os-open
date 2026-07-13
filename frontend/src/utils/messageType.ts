/**
 * 系统消息判断工具函数
 *
 * 只在数据转换层（messages.ts / session.ts）调用一次，
 * 将 role 修正为 'system'。下游展示层直接用 role === 'system' 判断。
 *
 * 后端标识来源：
 *   - routes_threads.py: record.type == "system" 时设置 record_type/type/sender_type = "system"
 *   - lifecycleHandlers.ts: handleSystemNotification 创建消息时设置完整 metadata
 */

/**
 * 判断消息是否为系统消息（基于 role + metadata 参数）
 *
 * 仅在 API 转换层调用，将 role 修正为 'system'。
 * 展示层（ChatContainer / MessageItem）直接检查 message.role === 'system'。
 */
export function checkIsSystemMessage(
  role?: string,
  metadata?: {
    record_type?: string
    type?: string
    sender_type?: string
    [key: string]: unknown
  },
): boolean {
  if (role === 'system') return true

  if (metadata) {
    if (
      metadata.record_type === 'system' ||
      metadata.type === 'system' ||
      metadata.sender_type === 'system'
    ) {
      return true
    }
  }

  return false
}
