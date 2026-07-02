/**
 * 流式事件路由解析
 */

/**
 * 解析事件的 pipeline_id
 *
 * 优先级：
 * 1. data.pipeline_id（非空字符串，最精确的路由键）
 * 2. eventData.pipeline_id（顶层字段，部分事件使用）
 *
 * pipeline_id 和 thread_id 是不同维度的字段：
 *   - pipeline_id: 管道标识，用于前端消息路由到正确的 pipeline tab
 *   - thread_id: 会话标识，用于后端连接管理
 *
 * 注意：不回退到 _threadId。在子管道场景下 thread_id 和 pipeline_id 不一致，
 * 回退到 _threadId 会导致消息路由到错误的标签页。因此当 pipeline_id 缺失时返回 null，
 * 由调用方 warn 并跳过，避免路由到错误位置。
 */
export function resolvePipelineId(eventData: any): string | null {
  // 优先级 1: data.pipeline_id（最精确的路由键）
  const dataPid = eventData.data?.pipeline_id
  if (typeof dataPid === 'string' && dataPid.length > 0) return dataPid

  // 优先级 2: 顶层 pipeline_id（部分事件在此字段传递）
  const topPid = eventData.pipeline_id
  if (typeof topPid === 'string' && topPid.length > 0) return topPid

  return null
}
