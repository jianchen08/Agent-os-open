/**
 * 流式事件路由解析
 */

import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useAgentTabStore } from '@/stores/agentTabStore'

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
 *
 * ADR 2026-08-21：原「未注册管道重定向到活跃管道」兜底已删除——spike 实证
 * 后端 resolve_pipeline_id_for_thread 校验归属并把外来 ID 回落到主管道，事件
 * 回流的 pipeline_id 恒为前端注册过的值，重定向防御场景不存在；而重定向会把
 * 别的管道内容灌进活跃视图（猜测型写入）。未注册管道事件由 isPipelineRelevant
 * 门控直接丢弃。
 */
export function resolvePipelineId(eventData: any): string | null {
  // 优先级 1: data.pipeline_id（最精确的路由键）
  const dataPid = eventData.data?.pipeline_id
  if (typeof dataPid === 'string' && dataPid.length > 0) {
    return dataPid
  }

  // 优先级 2: 顶层 pipeline_id（部分事件在此字段传递）
  const topPid = eventData.pipeline_id
  if (typeof topPid === 'string' && topPid.length > 0) {
    return topPid
  }

  return null
}

/**
 * 判断 pipeline 是否"被关注"（活跃/已注册/在 Tab 打开）。
 * 非关注 pipeline 的流式事件应被丢弃，不注册幽灵管道、不写 store。
 * ② 已注册但非活跃（如子任务管道）仍返回 true——保证子任务流式可见。
 */
export function isPipelineRelevant(pipelineId: string): boolean {
  if (!pipelineId) return false
  const ps = pipelineStore.getState()
  if (ps.activePipelineId === pipelineId) return true        // ① 当前活跃
  if (ps.pipelines?.[pipelineId]) return true                 // ② 已注册(含子任务)
  const tabs = useAgentTabStore.getState().tabs
  if (tabs?.some((t) => t.pipelineRunId === pipelineId)) return true  // ③ Tab 打开
  return false
}
