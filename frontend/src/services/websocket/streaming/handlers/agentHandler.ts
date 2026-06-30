/** 子 Agent 事件处理器 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { loggers } from '@/utils/logger'

const _debugLogger = loggers.websocket

/** 处理子 Agent 创建事件 注册顺序：管道元数据(含sessionId) → 映射 → Tab（仅当前会话） */
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

  // 而 navigateToPipeline 使用 sub-${pipelineId} 生成 tabId，每个管道唯一，
  // 两处不一致导致映射错乱。
  const tabId = `sub-${pipelineId}`
  const pStore = pipelineStore.getState()
  const agentTabStore = useAgentTabStore.getState()

  // 导致 pipelineSessionMap 映射错乱，点击标签跳转到对方任务的会话。
  // 仅注册 pipelineTabMap 映射，让紧随其后的 stream_start 用
  // threadId（后端 WS 事件自带的会话 ID）正确注册 pipelineMeta。
  const parentPipelineId = data.parentPipelineId
  let sessionId = ''

  _debugLogger.info(
    '[SUB_AGENT_CREATED] 开始查找 sessionId: pipelineId=%s parentPipelineId=%s activeSessionId=%s',
    pipelineId, parentPipelineId, useSessionStore.getState().activeSessionId,
  )

  // 优先级1: 通过 parentPipelineId 在 pipelineSessionMap 中查找父管道所属会话
  if (parentPipelineId && pStore.pipelineSessionMap[parentPipelineId]) {
    sessionId = pStore.pipelineSessionMap[parentPipelineId]
    _debugLogger.info(
      '[SUB_AGENT_CREATED] 优先级1 命中 pipelineSessionMap: sessionId=%s',
      sessionId,
    )
  }

  // 优先级2: 遍历所有 session.pipelineIds 查找父管道所属会话
  if (!sessionId && parentPipelineId) {
    const sessions = useSessionStore.getState().sessions
    const found = sessions.find(s => s.pipelineIds?.includes(parentPipelineId))
    if (found) {
      sessionId = found.id
      _debugLogger.info(
        '[SUB_AGENT_CREATED] 优先级2 命中 session.pipelineIds: sessionId=%s',
        sessionId,
      )
    }
  }

  // 导致 findPipelineLocation 三级查找都失败，用户点击子管道时报错"找不到管道归属"。
  if (!sessionId && parentPipelineId) {
    // 优先级3: parentPipelineId 存在但 pipelineSessionMap 和 session.pipelineIds 都没找到，
    // 使用 activeSessionId 作为 fallback（子管道通常属于当前活跃会话）
    sessionId = useSessionStore.getState().activeSessionId || ''
    if (sessionId) {
      _debugLogger.info(
        '[SUB_AGENT_CREATED] 使用 activeSessionId 作为 fallback: pipelineId=%s sessionId=%s',
        pipelineId, sessionId,
      )
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

  // pipelineMeta.sessionId 发现为 null，跨会话保护失效，activePipelineId
  // 被改成别的会话的管道，导致标签页跳转混乱。
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
