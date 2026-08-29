/** 全局管道导航服务 在所有会话的所有管道中查找并跳转到目标管道。 */

import { readSessions, forceReloadSessions } from '@/hooks/queries/useSessionsQuery'
import { reportError, ErrorType, ErrorSeverity } from '@/services/errorReporting'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore, type PipelineMeta } from '@/stores/pipelineMessageStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
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
    tabStore.pipelineTabMap[pipelineId] ||
    tabStore.tabs.find((t) => t.pipelineRunId === pipelineId)?.id ||
    null

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

  // 第三级：强制重拉会话列表后再查找（兜底）。必须绕过缓存——管道出生不触发
  // 会话列表失效（invalidateSessions 无事件源），缓存可能任意陈旧；拿缓存重查
  // 等于没查，任务运行期点开必报"找不到该管道"（直到某次后台重拉才自愈）。
  try {
    const refreshedSessions = await forceReloadSessions()
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
    reportError(`无法跳转到管道 ${pipelineId}：当前没有活跃会话`, {
      type: ErrorType.VALIDATION,
      severity: ErrorSeverity.WARNING,
      code: 'NAVIGATE_FAILED',
      source: 'frontend',
      component: 'pipelineNavigator',
      action: 'navigateToPipeline',
      pipelineId,
    })
    return false
  }

  // 管道归属解析（快速检查与后续导航共用一次查找）。残留绑定恰与目标管道
  // 相等时（Tab 持其他会话的管道 id），仅凭绑定相等判"已就位"是误判——视图
  // 仍停在当前会话，真实归属会话未被访问：归属确属他会话则不提前返回，继续
  // 正常导航流程（含跨会话切换）；归属未知（缓存与重查均未命中）保持信任
  // Tab 绑定的原语义。
  const location = await findPipelineLocation(pipelineId)
  const tabStore = useAgentTabStore.getState()
  const activeTab = tabStore.tabs.find((t) => t.id === tabStore.activeTabId)
  if (
    activeTab?.pipelineRunId === pipelineId &&
    (!location || location.sessionId === currentSid)
  ) {
    return true
  }

  if (!location) {
    reportError(
      `无法跳转到管道 ${pipelineId}：所有会话中都找不到该管道（会话列表可能已过期，请刷新后重试）`,
      {
        type: ErrorType.VALIDATION,
        severity: ErrorSeverity.WARNING,
        priority: 'high',
        code: 'NAVIGATE_FAILED',
        source: 'frontend',
        component: 'pipelineNavigator',
        action: 'navigateToPipeline',
        pipelineId,
      },
    )
    return false
  }
  const targetSessionId = location.sessionId

  // 如果在其他会话，先切换会话
  if (targetSessionId !== currentSid) {
    const sessions = readSessions()
    const sessionExists = sessions.some((s) => s.id === targetSessionId)
    if (sessionExists) {
      useAgentTabStore.getState().saveCurrentTabs()
      await useSessionListStore.getState().setActiveSession(targetSessionId)
      useAgentTabStore.getState().initSessionTabs(targetSessionId)
    }
    // session 不存在时中止（数据不一致，拒绝在当前会话创建幽灵标签）
    if (!sessionExists) {
      reportError(
        `无法跳转到管道 ${pipelineId}：其所属会话 ${targetSessionId} 已不存在（数据不一致）`,
        {
          type: ErrorType.VALIDATION,
          severity: ErrorSeverity.WARNING,
          priority: 'high',
          code: 'NAVIGATE_FAILED',
          source: 'frontend',
          component: 'pipelineNavigator',
          action: 'navigateToPipeline',
          pipelineId,
        },
      )
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
  const targetSession = readSessions().find((s) => s.id === targetSessionId)
  // 主管道判定（权威 activePipelineId 解析，不按 [0] 位置猜测）
  const isMainPipeline = !!targetSession && mainPipelineIdOf(targetSession) === pipelineId
  if (isMainPipeline) {
    const mainTab = currentTabStore.tabs.find((t) => t.id === `main-${targetSessionId}`)
    if (mainTab) {
      currentTabStore.switchToTab(mainTab.id)
      return true
    }
    reportError(
      `无法跳转到管道 ${pipelineId}：会话 ${targetSessionId} 的主标签缺失（数据不一致）`,
      {
        type: ErrorType.VALIDATION,
        severity: ErrorSeverity.WARNING,
        priority: 'high',
        code: 'NAVIGATE_FAILED',
        source: 'frontend',
        component: 'pipelineNavigator',
        action: 'navigateToPipeline',
        pipelineId,
        tabs: currentTabStore.tabs.map((t) => ({
          id: t.id,
          level: t.agentLevel,
          pid: t.pipelineRunId,
        })),
      },
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
