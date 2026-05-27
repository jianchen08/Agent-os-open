/**
 * 子 Agent 事件处理器
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { loggers } from '@/utils/logger'

const _debugLogger = loggers.websocket

/**
 * 处理子 Agent 创建事件
 *
 * 注册顺序：管道元数据(含sessionId) → 映射 → Tab
 * 必须先注册 pipelineMeta（含 sessionId），否则紧随其后的 stream_start 事件
 * 在 shouldActivatePipeline 中找不到 sessionId，跨会话保护失效，导致 activePipelineId 被篡改。
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
  const pStore = pipelineStore.getState()
  const agentTabStore = useAgentTabStore.getState()

  // BUG-FIX-fix_20260528_cross_pipeline_jump:
  // 问题根因: registerPipeline(含 sessionId) 放在 registerPipelineTab/openSubAgentTab 之后，
  //          sub_agent_created 和 stream_start 几乎同时到达时，streamHandler 检查
  //          pipelineMeta.sessionId 发现为 null，跨会话保护失效，activePipelineId
  //          被改成别的会话的管道，导致标签页跳转混乱。
  // 修复方案: 把 registerPipeline 提前到最前面，确保 pipelineMeta.sessionId 在
  //          stream_start 到达前就已存在。
  // 影响范围: 子Agent创建时 stream_start 事件的跨会话保护
  // 修复日期: 2026-05-28
  if (!pStore.pipelines[pipelineId]) {
    const sessionId =
      data.sessionId
      || pStore.pipelineSessionMap[parentId || pStore.activePipelineId || '']
      || useSessionStore.getState().activeSessionId
      || ''
    pStore.registerPipeline({
      pipelineId,
      sessionId,
      level: 2,
      tabId,
      agentName,
      status: 'running',
      parentId: parentId || pStore.activePipelineId,
      unreadCount: 0,
    })
  }

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
}
