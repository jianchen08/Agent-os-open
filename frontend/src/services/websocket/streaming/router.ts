/**
 * 流式事件路由解析
 */

/**
 * 解析事件的 pipeline_id
 *
 * 优先级：data.pipeline_id（非空字符串）> null
 *
 * ⚠️ 重要：pipeline_id 和 thread_id 是不同维度的字段，严禁混用：
 *   - pipeline_id: 管道标识，用于前端消息路由到正确的 pipeline tab
 *   - thread_id: 会话标识，用于后端连接管理
 *   混用会导致子管道消息路由到主管道，造成消息串扰。
 *
 * BUG-FIX-fix_20260511_message_cross_talk:
 * 问题根因: 后端 TargetedSink 定向路由失败时回退到 broadcast_event()，
 *          事件被发送到所有 WebSocket 连接。每个连接池连接会为事件打上自己的 _threadId。
 *          原逻辑 data.pipeline_id || _threadId || null 会在 pipeline_id 为空字符串时
 *          走到 _threadId fallback，导致同一事件被路由到不同的管道，造成消息串扰。
 * 修复方案: 严格校验 pipeline_id（空字符串视为无效），不再使用 _threadId 作为 fallback。
 *          _threadId 仅用于 streamingStore 的双 key 配对（与 handleStreamStart 配合），
 *          不参与消息路由。
 */
export function resolvePipelineId(eventData: any): string | null {
  const pid = eventData.data?.pipeline_id
  return (typeof pid === 'string' && pid.length > 0) ? pid : null
}
