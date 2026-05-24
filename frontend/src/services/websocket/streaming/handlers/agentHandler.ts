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
 * 统一流程：注册映射 → 创建 Tab → 注册管道元数据
 * 主/子管道无区别，均通过 pipeline_id 路由。
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
  const agentTabStore = useAgentTabStore.getState()

  // 注册映射 + 创建 Tab + 注册管道元数据（三步合一）
  agentTabStore.registerPipelineTab(pipelineId, tabId)
  agentTabStore.openSubAgentTab({
    agentId: taskId,
    agentName,
    parentRecordId: parentId || taskId,
    agentLevel: 2,
    taskId,
    status: 'running',
    setActive: false,
    pipelineId,
  })

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
