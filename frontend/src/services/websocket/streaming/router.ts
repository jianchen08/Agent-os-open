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
 */
export function resolvePipelineId(eventData: any): string | null {
  // 优先级 1: data.pipeline_id（最精确的路由键）
  const dataPid = eventData.data?.pipeline_id
  if (typeof dataPid === 'string' && dataPid.length > 0) {
    return resolveActiveSessionPipeline(dataPid, eventData)
  }

  // 优先级 2: 顶层 pipeline_id（部分事件在此字段传递）
  const topPid = eventData.pipeline_id
  if (typeof topPid === 'string' && topPid.length > 0) {
    return resolveActiveSessionPipeline(topPid, eventData)
  }

  return null
}

/**
 * 将"未注册但属于当前活跃会话"的 pipeline_id 重定向到活跃 pipeline。
 *
 * 场景：用户在生成中点 Stop（abort）后，后端可能为下一轮响应分配一个全新的
 * pipeline_id。前端占位气泡创建在活跃 pipeline（会话主管道）上，若直接用新
 * pipeline_id 路由，事件会被 isPipelineRelevant 门控丢弃（index.ts 顶层 gate +
 * streamHandler 二次 gate），占位气泡永远卡在"思考中"。
 *
 * 重定向后所有 stream 事件（start/chunk/end/thinking/tool…）一致落到活跃 pipeline，
 * 占位气泡获得内容、stream_end 正常收尾。此函数同时被 index.ts 顶层 gate 和各
 * handler 调用，是覆盖全部事件类型的单一最优点。
 *
 * 仅对"未关注"的 pipeline 生效：已注册的子任务/Tab pipeline 原样返回，不受影响。
 * 注意：不把 _threadId 当作 pipeline_id 回退——这里仅用 threadId 判断会话归属，
 * 路由目标始终是 activePipelineId（会话主管道），不会串到错误 Tab。
 */
function resolveActiveSessionPipeline(pipelineId: string, eventData: any): string {
  // 已关注（活跃/已注册/已开 Tab）→ 原样返回（含子任务管道）
  if (isPipelineRelevant(pipelineId)) return pipelineId

  const ps = pipelineStore.getState()
  const activePid = ps.activePipelineId
  if (!activePid) return pipelineId
  // 仅在活跃 pipeline 正在等待流式（有占位气泡 / streamingState 活跃）时重定向，
  // 精确命中"发送后等待响应"场景，避免误吞其他会话的未注册 pipeline 事件。
  if (!ps.streamingState?.[activePid]?.isStreaming) return pipelineId
  const threadId = eventData?.data?._threadId || eventData?._threadId
  if (!threadId) return pipelineId
  const activeSession = ps.pipelineSessionMap?.[activePid]
  // 事件的 thread_id 与活跃 pipeline 所属 session 一致 → 重定向到活跃 pipeline
  if (activeSession && threadId === activeSession) {
    return activePid
  }
  return pipelineId
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
