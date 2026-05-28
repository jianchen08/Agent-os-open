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
 * 注册顺序：管道元数据(含sessionId) → 映射 → Tab（仅当前会话）
 * 必须先注册 pipelineMeta（含 sessionId），否则紧随其后的 stream_start 事件
 * 在 shouldActivatePipeline 中找不到 sessionId，跨会话保护失效，导致 activePipelineId 被篡改。
 * 子标签只在所属会话的 Tab 列表中创建，非当前会话的子标签仅注册管道元数据和映射，不创建 Tab。
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

  const sessionId =
    data.sessionId
    || pStore.pipelineSessionMap[parentId || pStore.activePipelineId || '']
    || useSessionStore.getState().activeSessionId
    || ''

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

  // 注册 pipeline→tab 映射（所有会话都注册，用于 WebSocket 消息路由）
  agentTabStore.registerPipelineTab(pipelineId, tabId)

  // BUG-FIX-fix_20260528_tab_wrong_session:
  // 问题根因: 子标签无条件创建在当前 agentTabStore.tabs 中，但 agentTabStore.tabs
  //          是按会话隔离的（通过 currentSessionId + localStorage）。当用户在会话 B
  //          但收到会话 A 的 sub_agent_created 事件时，会话 A 的子标签被错误地
  //          创建在会话 B 的 Tab 列表中，saveCurrentTabs() 后持久化到错误的 localStorage key。
  //          导致切换回会话 A 时看不到该子标签，而会话 B 中出现了不属于它的标签。
  // 修复方案: 只在子标签属于当前活跃会话时才调用 openSubAgentTab 创建标签；
  //          非当前会话的子标签仅注册映射和管道元数据，等用户切换到对应会话时
  //          通过 initSessionTabs 从 localStorage 恢复（或通过 pipelineNavigator 按需创建）。
  // 影响范围: 多会话并行时子标签的会话归属正确性
  // 修复日期: 2026-05-28
  const currentSessionId = agentTabStore.currentSessionId
  if (sessionId && currentSessionId && sessionId === currentSessionId) {
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
  } else {
    _debugLogger.info(
      `[SUB_AGENT_CREATED] skipping openSubAgentTab: pipeline belongs to session %s, current session is %s`,
      sessionId?.slice(0, 12) || '(empty)',
      currentSessionId?.slice(0, 12) || '(empty)',
    )
  }
}
