/**
 * 流式事件路由解析
 */

/**
 * 解析事件的 pipeline_id
 *
 * 优先级：
 * 1. data.pipeline_id（非空字符串，最精确的路由键）
 * 2. eventData.pipeline_id（顶层字段，部分事件使用）
 * 3. _threadId / data._threadId（最后回退）
 *
 * pipeline_id 和 thread_id 是不同维度的字段：
 *   - pipeline_id: 管道标识，用于前端消息路由到正确的 pipeline tab
 *   - thread_id: 会话标识，用于后端连接管理
 *
 * FIX: 添加 pipeline_id 顶层字段检查和 _threadId 回退。
 * 当后端在 tool_result / stream_start 等事件中遗漏 pipeline_id 时，
 * 通过 _threadId 回退到正确的管道，避免事件被丢弃导致前端渲染中断。
 */
export function resolvePipelineId(eventData: any): string | null {
  // 优先级 1: data.pipeline_id（最精确的路由键）
  const dataPid = eventData.data?.pipeline_id
  if (typeof dataPid === 'string' && dataPid.length > 0) return dataPid

  // 优先级 2: 顶层 pipeline_id（部分事件在此字段传递）
  const topPid = eventData.pipeline_id
  if (typeof topPid === 'string' && topPid.length > 0) return topPid

  // 优先级 3: 回退到 _threadId（工具调用完成后继续输出时的安全回退）
  const threadId = eventData._threadId || eventData.data?._threadId
  if (typeof threadId === 'string' && threadId.length > 0) return threadId

  return null
}
