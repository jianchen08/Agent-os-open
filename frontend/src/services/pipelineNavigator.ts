/** 全局管道导航服务 在所有会话的所有管道中查找并跳转到目标管道。 */

import { usePipelineMessageStore, type PipelineMeta } from '@/stores/pipelineMessageStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import type { AgentTab } from '@/types/task'

/** 查找结果 */
export interface PipelineLocation {
  /** 管道所属会话 ID */
  sessionId: string
  /** 管道 ID */
  pipelineId: string
  /** 对应的 Tab ID（null 表示该会话尚未加载该管道的标签） */
  tabId: string | null
}

/** 在所有会话中查找管道归属 查找优先级： */
export async function findPipelineLocation(pipelineId: string): Promise<PipelineLocation | null> {
  const pipelineStore = usePipelineMessageStore.getState()
  const tabStore = useAgentTabStore.getState()
  // 统一查找 tabId：先查 pipelineTabMap，再查 tab.pipelineRunId
  const resolveTabId = () =>
    tabStore.pipelineTabMap[pipelineId]
    || tabStore.tabs.find((t) => t.pipelineRunId === pipelineId)?.id
    || null

  // 第二级（提前执行）：遍历所有会话的 pipelineIds（后端权威数据）
  const sessions = useSessionStore.getState().sessions
  let authoritativeSessionId: string | null = null
  for (const session of sessions) {
    if (session.pipelineIds && session.pipelineIds.includes(pipelineId)) {
      authoritativeSessionId = session.id
      break
    }
  }

  // 第一级：内存中的 pipelineSessionMap（最快，但可能过时）
  const cachedSessionId = pipelineStore.pipelineSessionMap[pipelineId]
  if (cachedSessionId) {
    if (authoritativeSessionId && authoritativeSessionId !== cachedSessionId) {
      // 缓存与权威数据不一致，修正缓存
      pipelineStore.registerPipeline({
        pipelineId,
        sessionId: authoritativeSessionId,
        level: 2,
        tabId: resolveTabId(),
        agentName: '',
        status: 'running',
        parentId: null,
        unreadCount: 0,
      })
      return { sessionId: authoritativeSessionId, pipelineId, tabId: resolveTabId() }
    }
    return { sessionId: cachedSessionId, pipelineId, tabId: resolveTabId() }
  }

  // 第二级结果直接使用（已提前计算）
  if (authoritativeSessionId) {
    return { sessionId: authoritativeSessionId, pipelineId, tabId: resolveTabId() }
  }

  // 第三级：重新拉取会话列表后再查找（兜底）
  try {
    await useSessionListStore.getState().fetchSessions({ background: true })
    const refreshedSessions = useSessionStore.getState().sessions
    for (const session of refreshedSessions) {
      if (session.pipelineIds && session.pipelineIds.includes(pipelineId)) {
        return { sessionId: session.id, pipelineId, tabId: null }
      }
    }
  } catch (e) {
    console.error('[findPipelineLocation] fetchSessions API 调用失败', e)
  }

  return null
}

/** 全局导航到指定管道 统一逻辑：通过 pipeline_id 在所有会话的所有标签中查找， */
export async function navigateToPipeline(
  pipelineId: string,
  options?: {
    agentName?: string
    agentLevel?: 1 | 2 | 3
    taskId?: string
    status?: string
  },
): Promise<boolean> {
  const { agentName = '子任务', agentLevel = 2, taskId, status = 'running' } = options || {}

  const currentSid = useSessionStore.getState().activeSessionId
  if (!currentSid) {
    console.error('[navigateToPipeline] 无活跃会话，无法导航到管道', pipelineId)
    return false
  }

  // 快速检查：当前标签的 pipelineRunId 已经是目标管道，直接返回
  const tabStore = useAgentTabStore.getState()
  const activeTab = tabStore.tabs.find((t) => t.id === tabStore.activeTabId)
  if (activeTab?.pipelineRunId === pipelineId) {
    return true
  }

  const location = await findPipelineLocation(pipelineId)
  if (!location) {
    console.error('[navigateToPipeline] 找不到管道归属，拒绝降级到当前会话: pipelineId=%s', pipelineId)
    return false
  }
  const targetSessionId = location.sessionId

  // 如果在其他会话，先切换会话
  if (targetSessionId !== currentSid) {
    const sessions = useSessionStore.getState().sessions
    const sessionExists = sessions.some(s => s.id === targetSessionId)
    if (sessionExists) {
      useAgentTabStore.getState().saveCurrentTabs()
      await useSessionListStore.getState().setActiveSession(targetSessionId)
      useAgentTabStore.getState().initSessionTabs(targetSessionId)
    }
    // session 不存在时中止（数据不一致，拒绝在当前会话创建幽灵标签）
    if (!sessionExists) {
      console.error('[navigateToPipeline] 目标会话已不存在: sessionId=%s pipelineId=%s', targetSessionId, pipelineId)
      return false
    }
  }

  // 刷新 tabStore 引用（会话切换后状态已更新）
  const currentTabStore = useAgentTabStore.getState()

  // 统一查找已有标签：通过 pipelineRunId 匹配
  const existingTab = currentTabStore.tabs.find((t) => t.pipelineRunId === pipelineId)
  if (existingTab) {
    currentTabStore.switchToTab(existingTab.id)
    return true
  }

  // 创建新标签
  const tabId = `sub-${pipelineId}`

  const pipelineStore = usePipelineMessageStore.getState()
  if (!pipelineStore.pipelines[pipelineId]) {
    pipelineStore.registerPipeline({
      pipelineId,
      sessionId: targetSessionId,
      level: agentLevel,
      tabId,
      agentName,
      status: status as PipelineMeta['status'],
      parentId: targetSessionId,
      unreadCount: 0,
    })
  }

  currentTabStore.openSubAgentTab({
    agentId: taskId || pipelineId,
    agentName,
    parentRecordId: pipelineId,
    agentLevel,
    taskId,
    status: status as AgentTab['status'],
    setActive: true,
    pipelineId,
  })

  currentTabStore.loadTabMessages(tabId, pipelineId)

  return true
}
