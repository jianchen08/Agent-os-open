/**
 * 子 Agent 事件处理器
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

const _debugLogger = loggers.websocket

/**
 * 处理子 Agent 创建事件
 *
 * FIX: 子 Agent 创建时同时注册 pipelineMessageStore 的管道元数据，
 * 避免 stream_start 虽调用 addMessage 但管道元数据缺失导致 chunk 无法正确路由。
 */
export function handleSubAgentCreated(eventData: any) {
  const data = eventData.data || eventData
  const taskId = data.taskId || data.agentId
  const pipelineId = data.pipelineId
  const parentId = data.parentId
  const agentName = data.agentName || 'Sub-agent'
  _debugLogger.info(
    `[SUB_AGENT_CREATED] taskId=%s pipelineId=%s parentId=%s`,
    taskId, pipelineId, parentId,
  )
  if (!taskId || !pipelineId) return

  const tabId = `sub-${parentId || taskId}`
  useAgentTabStore.getState().registerPipelineTab(pipelineId, tabId)

  const state = pipelineStore.getState()
  if (!state.pipelines[pipelineId]) {
    state.registerPipeline({
      pipelineId,
      sessionId: data.sessionId || eventData.data?._threadId || eventData._threadId || '',
      level: 2,
      tabId,
      agentName,
      status: 'running',
      parentId: parentId || state.activePipelineId,
      unreadCount: 0,
    })
  }
}
