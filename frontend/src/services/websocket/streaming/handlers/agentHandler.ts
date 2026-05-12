/**
 * 子 Agent 事件处理器
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { loggers } from '@/utils/logger'

const _debugLogger = loggers.websocket

/**
 * 处理子 Agent 创建事件
 */
export function handleSubAgentCreated(eventData: any) {
  const data = eventData.data || eventData
  const taskId = data.taskId || data.agentId
  const pipelineId = data.pipelineId
  const parentId = data.parentId
  _debugLogger.info(
    `[SUB_AGENT_CREATED] taskId=%s pipelineId=%s parentId=%s`,
    taskId, pipelineId, parentId,
  )
  if (!taskId || !pipelineId) return

  const tabId = `sub-${parentId || taskId}`
  useAgentTabStore.getState().registerPipelineTab(pipelineId, tabId)
}
