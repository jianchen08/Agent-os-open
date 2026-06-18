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

  // BUG-FIX-fix_20260607_cross_task_tab_jump:
  // 问题根因: tabId 使用 parentId 生成（sub-${parentId}），同父管道的多个子任务共享同一个 tabId，
  //          导致 registerPipelineTab 互相覆盖，点击标签时跳转到对方任务的会话。
  //          而 navigateToPipeline 使用 sub-${pipelineId} 生成 tabId，每个管道唯一，
  //          两处不一致导致映射错乱。
  // 修复方案: 统一使用 pipelineId 生成 tabId（sub-${pipelineId}），与 navigateToPipeline 保持一致，
  //          确保每个子管道有独立的标签页。
  // 影响范围: 同一会话下多个子任务的标签页跳转
  // 修复日期: 2026-06-07
  const tabId = `sub-${pipelineId}`
  const pStore = pipelineStore.getState()
  const agentTabStore = useAgentTabStore.getState()

  // BUG-FIX-fix_20260607_cross_session_pipeline_jump:
  // 问题根因: 后端 sub_agent_created 事件不包含 sessionId，原回退链依赖
  //          activeSessionId，当用户正在查看其他会话时，新管道会被注册到错误的会话，
  //          导致 pipelineSessionMap 映射错乱，点击标签跳转到对方任务的会话。
  // 修复方案: 优先使用后端新增的 parentPipelineId 字段查找父管道所属会话，
  //          再遍历 session.pipelineIds 兜底。找不到时不注册 pipelineMeta，
  //          仅注册 pipelineTabMap 映射，让紧随其后的 stream_start 用
  //          threadId（后端 WS 事件自带的会话 ID）正确注册 pipelineMeta。
  // 影响范围: 不同会话的子任务标签页跳转准确性
  // 修复日期: 2026-06-07
  const parentPipelineId = data.parentPipelineId
  let sessionId = ''

  // 优先级1: 通过 parentPipelineId 在 pipelineSessionMap 中查找父管道所属会话
  if (parentPipelineId && pStore.pipelineSessionMap[parentPipelineId]) {
    sessionId = pStore.pipelineSessionMap[parentPipelineId]
  }

  // 优先级2: 遍历所有 session.pipelineIds 查找父管道所属会话
  if (!sessionId && parentPipelineId) {
    const sessions = useSessionStore.getState().sessions
    const found = sessions.find(s => s.pipelineIds?.includes(parentPipelineId))
    if (found) {
      sessionId = found.id
    }
  }

  // 注册 pipeline→tab 映射（不依赖 sessionId，所有会话都注册，用于 WebSocket 消息路由）
  agentTabStore.registerPipelineTab(pipelineId, tabId)

  // 无法确定会话归属时不注册 pipelineMeta，避免写入错误的 pipelineSessionMap 映射。
  // 紧随其后的 stream_start 事件会用 threadId（后端 WS 自带的会话 ID）正确注册。
  if (!sessionId) {
    _debugLogger.warn(
      `[SUB_AGENT_CREATED] 无法确定管道所属会话，跳过 pipelineMeta 注册: pipelineId=%s parentPipelineId=%s`,
      pipelineId, parentPipelineId,
    )
    return
  }

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
}
