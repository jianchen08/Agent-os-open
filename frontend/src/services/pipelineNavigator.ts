/** 全局管道导航服务 在所有会话的所有管道中查找并跳转到目标管道。 */

import { usePipelineMessageStore, type PipelineMeta } from '@/stores/pipelineMessageStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useSessionStore } from '@/stores/sessionStore'
import { readSessions, ensureSessionsLoaded } from '@/hooks/queries/useSessionsQuery'
import { useSessionListStore } from '@/stores/sessionListStore'
import { mainPipelineIdOf } from '@/utils/mappers'
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
  const sessions = readSessions()
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
    await ensureSessionsLoaded()
    const refreshedSessions = readSessions()
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
    const sessions = readSessions()
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

  // 统一查找已有标签：通过 pipelineRunId 匹配（主标签 main-xxx 与子标签都在 tabs 中）
  const existingTab = currentTabStore.tabs.find((t) => t.pipelineRunId === pipelineId)
  if (existingTab) {
    currentTabStore.switchToTab(existingTab.id)
    return true
  }

  // 主管道特判：会话主标签（main-${sessionId}）不建子标签——主标签是会话创建时
  // 固定存在的不变量（initSessionTabs 保证），走到缺失分支即数据不一致 bug，
  // 显式报错暴露，不做静默兜底（静默激活会掩盖根因）。
  const targetSession = readSessions().find(
    (s) => s.id === targetSessionId,
  )
  // 主管道判定（权威 activePipelineId 解析，不按 [0] 位置猜测）
  const isMainPipeline =
    !!targetSession && mainPipelineIdOf(targetSession) === pipelineId
  if (isMainPipeline) {
    const mainTab = currentTabStore.tabs.find((t) => t.id === `main-${targetSessionId}`)
    if (mainTab) {
      currentTabStore.switchToTab(mainTab.id)
      return true
    }
    console.error(
      '[navigateToPipeline] 主标签缺失（数据不一致 bug）: sessionId=%s pipelineId=%s tabs=%o',
      targetSessionId,
      pipelineId,
      currentTabStore.tabs.map((t) => ({ id: t.id, level: t.agentLevel, pid: t.pipelineRunId })),
    )
    return false
  }

  // 子管道：创建新标签
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
