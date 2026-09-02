/** 会话列表状态管理 Store（服务端状态 query 化）
 *
 * 数据容器已换 TanStack Query 缓存（hooks/queries/useSessionsQuery）：
 * sessions 的读写全部经 readSessions/updateSessionsCache/invalidateSessions，
 * 本 store 只承载编排逻辑（乐观更新+回滚、跨 store 副作用、恢复链）。
 * fetchSessions 已退役——列表拉取由 useSessionsQuery 承担（缓存秒开+后台刷新），
 * 刷新后的 last_active_session 恢复改走 restoreActiveSessionIfNeeded（由
 * HomePage 在 query 数据到位时调用一次）。
 */

import { create } from 'zustand'
import {
  createSession as createSessionApi,
  deleteSession as deleteSessionApi,
  updateSessionAgent as updateSessionAgentApi,
  updateSession as updateSessionApi,
} from '@/services/api/session'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { clearSessionExecutionOptions } from '@/services/sessionExecutionOptions'
import { loggers } from '@/utils/logger'
import { uiStorage, STORAGE_KEYS } from '@/utils/storage'
import { useAgentStore } from './agentStore'
import { useAgentTabStore } from './agentTabStore'
import { useNotificationStore } from './notificationStore'
import { usePipelineMessageStore } from './pipelineMessageStore'
import { useSessionStore } from './sessionStore'
import { readSessions, updateSessionsCache } from '@/hooks/queries/useSessionsQuery'
import { readAgents } from '@/hooks/queries/useAgentsQuery'
import { mainPipelineIdOf } from '@/utils/mappers'
import type { Session } from '@/types/models'

const logger = loggers.sessionStore

interface CreateSessionOptions {
  agentId?: string
  /**
   * 插件表单值整包（metadata 存储形状，键由各插件 thread_fields 的
   * x_metadata_key 声明，modal 层映射；本 store 与 API 层不感知具体字段）。
   */
  fieldMetadata?: Record<string, string>
}

interface SessionListState {
  /** query 数据到位后恢复上次选中会话（幂等：已有有效选中时不动作） */
  restoreActiveSessionIfNeeded: (sessions: Session[]) => Promise<void>
  createSession: (title?: string, options?: CreateSessionOptions) => Promise<Session>
  deleteSession: (id: string) => Promise<void>
  setActiveSession: (id: string, fetchData?: boolean) => Promise<void>
  updateSession: (sessionId: string, updates: Partial<Session>) => void
  toggleSessionPin: (sessionId: string) => void
  updateSessionAgent: (sessionId: string, agentId: string | null) => Promise<void>
  toggleSessionStar: (sessionId: string) => void
  renameSession: (sessionId: string, newTitle: string) => void
  searchSessions: (keyword: string) => Session[]
  copySession: (sessionId: string) => Promise<Session>
  /** 首次 AI 回复完成后，根据首条用户消息自动重命名会话 */
  autoRenameSessionIfNeeded: (sessionId: string, pipelineId: string) => void
}

/** 默认主 Agent 名称 */
const DEFAULT_AGENT_NAME = '灵汐'

/** 生成默认会话标题，使用主 Agent 名称。 */
const generateSessionTitle = (): string => {
  return DEFAULT_AGENT_NAME
}

export const useSessionListStore = create<SessionListState>()((_, get) => ({
  restoreActiveSessionIfNeeded: async (sessions: Session[]) => {
    const validSessionIds = new Set(sessions.map((s) => s.id))
    const currentActive = useSessionStore.getState().activeSessionId
    // 幂等保护：已有有效选中（含后台刷新重跑本函数）直接返回
    if (currentActive && validSessionIds.has(currentActive)) {
      return
    }
    const savedSessionId = uiStorage.getLastActiveSession()
    if (savedSessionId && validSessionIds.has(savedSessionId)) {
      await get().setActiveSession(savedSessionId)
    }
  },

  createSession: async (title?: string, options?: CreateSessionOptions) => {
    const sessionTitle = title || generateSessionTitle()

    const newSession = await createSessionApi({
      title: sessionTitle,
      agentId: options?.agentId,
      fieldMetadata: options?.fieldMetadata,
    })

    updateSessionsCache((prev) => [...prev, newSession])
    useSessionStore.setState({ activeSessionId: newSession.id })

    // 会话面初始化与 setActiveSession 同款：重建 agentTabStore 标签面（顶部
    // 对话标签随 activeTabId 切换、ChatInput 随 key 重建草稿状态）+ 持久化
    // 选中会话。缺失会导致创建会话后消息区已切到新管道、但顶部标签与输入框
    // 状态仍停留上一会话（需手动切换才对齐）。
    useAgentTabStore.getState().initSessionTabs(newSession.id)
    uiStorage.setLastActiveSession(newSession.id)

    if (newSession.activePipelineId) {
      const pipelineStore = usePipelineMessageStore.getState()
      pipelineStore.registerPipeline({
        pipelineId: newSession.activePipelineId,
        sessionId: newSession.id,
        level: 1,
        tabId: null,
        agentName: '',
        status: 'idle',
        parentId: null,
        unreadCount: 0,
      })
      pipelineStore.activatePipeline(newSession.activePipelineId)
      logger.info(
        '[createSession] pipeline registered: sessionId=%s pipelineId=%s',
        newSession.id.slice(0, 12),
        newSession.activePipelineId.slice(0, 12),
      )
    }

    return newSession
  },

  /** 删除会话（含完整清理） */
  deleteSession: async (id: string) => {
    useSessionStore.setState((state) => ({
      deletingSessionIds: new Set(state.deletingSessionIds).add(id),
    }))

    try {
      // 1. 收集该会话的所有管道ID，逐个发送取消信号
      const pipelineStore = usePipelineMessageStore.getState()
      const allPipelineIds = Object.entries(pipelineStore.pipelineSessionMap)
        .filter(([, sessionId]) => sessionId === id)
        .map(([pipelineId]) => pipelineId)
      for (const pid of allPipelineIds) {
        globalWS.sendCancel(id, '会话已删除', pid)
      }

      // 2. 查找所有属于该会话的 pipelineId（主管道 + 子管道 + 孙管道）
      // sessionId 本身也可能是主管道 ID
      if (!allPipelineIds.includes(id)) {
        allPipelineIds.push(id)
      }

      // 3. 停止所有管道的流式传输
      for (const pipelineId of allPipelineIds) {
        pipelineStore.stopStreaming(pipelineId)
      }

      // 4. 清理 pipelineMessageStore 中所有相关管道的数据
      const {
        messagesByPipeline: curMessages,
        pipelines: curPipelines,
        pipelineSessionMap: curSessionMap,
        streamingState: curStreaming,
        topCursorsByPipeline: curTopCursors,
        bottomCursorsByPipeline: curBottomCursors,
        hasMoreOlderByPipeline: curHasMore,
        isLoadingOlderByPipeline: curLoadingMore,
      } = pipelineStore

      const removeSet = new Set(allPipelineIds)
      const filterByKey = <T>(record: Record<string, T>): Record<string, T> => {
        const result: Record<string, T> = {}
        for (const [key, value] of Object.entries(record)) {
          if (!removeSet.has(key)) {
            result[key] = value
          }
        }
        return result
      }

      usePipelineMessageStore.setState({
        messagesByPipeline: filterByKey(curMessages),
        pipelines: filterByKey(curPipelines),
        pipelineSessionMap: filterByKey(curSessionMap),
        streamingState: filterByKey(curStreaming),
        topCursorsByPipeline: filterByKey(curTopCursors),
        bottomCursorsByPipeline: filterByKey(curBottomCursors),
        hasMoreOlderByPipeline: filterByKey(curHasMore),
        isLoadingOlderByPipeline: filterByKey(curLoadingMore),
      })

      // 5. 清理 agentTabStore（标签页、映射、localStorage）
      const agentTabStore = useAgentTabStore.getState()
      if (agentTabStore.currentSessionId === id) {
        agentTabStore.resetAllTabs()
        try {
          localStorage.removeItem(`agent-tabs-${id}`)
        } catch {
          // localStorage 清理失败不影响主流程
        }
      }

      // 6. 调用后端删除 API
      await deleteSessionApi(id)
      // 执行选项本地快照随会话删除（残留键会被同名会话覆写，但显式清理更干净）
      clearSessionExecutionOptions(id)

      // 7. 更新缓存与会话选中态
      updateSessionsCache((prev) => prev.filter((session) => session.id !== id))
      useSessionStore.setState((state) => {
        const newDeletingIds = new Set(state.deletingSessionIds)
        newDeletingIds.delete(id)

        return {
          activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
          deletingSessionIds: newDeletingIds,
        }
      })

      // 删除当前活跃会话时清理持久化的会话ID
      if (!useSessionStore.getState().activeSessionId) {
        try { localStorage.removeItem(STORAGE_KEYS.LAST_ACTIVE_SESSION) } catch (_e) { /* localStorage 清理失败不影响主流程 */ }
      }
    } catch (error: any) {
      const errorMessage = error.message || '删除会话失败'
      useSessionStore.setState((state) => {
        const newDeletingIds = new Set(state.deletingSessionIds)
        newDeletingIds.delete(id)
        return { deletingSessionIds: newDeletingIds }
      })
      throw new Error(errorMessage)
    }
  },

  setActiveSession: async (id: string, fetchData: boolean = true) => {
    if (!id || id.trim().length === 0) {
      return
    }

    const sessions = readSessions()
    const session = sessions.find((s) => s.id === id)
    if (!session) {
      return
    }

    // s.activePipelineId，显示"老数据"（上一个会话的消息）。
    useAgentTabStore.getState().initSessionTabs(id)

    useSessionStore.setState({ activeSessionId: id })
    // 持久化当前活跃会话ID，页面刷新后可恢复
    uiStorage.setLastActiveSession(id)

    if (session.agentId) {
      const agents = readAgents()
      const matchedAgent = agents.find(
        (a) => a.id === session.agentId || a.configId === session.agentId,
      )
      if (matchedAgent) {
        useAgentStore.getState().setCurrentAgentId(matchedAgent.id)
      }
    }

    if (fetchData) {
      try {
        // 主管道权威解析：activePipelineId 优先，不按 [0] 位置猜测
        const pipelineId = mainPipelineIdOf(session)
        if (!pipelineId) {
          console.error('[setActiveSession] 会话缺少主管道: sessionId=%s pipelineIds=%o', id, session.pipelineIds)
        }
        if (pipelineId) {
          // 统一加载入口：流式保护 + 双游标决策（init/after_sequence）已收敛到
          // loadPipelineMessages 内部，会话切换走默认 mode='auto'。
          await usePipelineMessageStore.getState().loadPipelineMessages(pipelineId, { threadId: id })
        }
      } catch (error) {
        console.error('[setActiveSession] 加载会话数据失败:', error)
      }
      // 注意：切换会话只刷新消息区域，工作区（FileTreeWidget 等）保持不动。
      // 不调用 bumpWorkspaceDataVersion —— 工作区数据刷新由 useRealtimeEvents
      // 在任务状态变化（task_status_changed 等事件）时触发，会话切换不应牵连工作区。
    }
  },

  updateSession: (sessionId: string, updates: Partial<Session>) => {
    updateSessionsCache((prev) =>
      prev.map((session) =>
        session.id === sessionId
          ? { ...session, ...updates, updatedAt: new Date().toISOString() }
          : session,
      ),
    )
  },

  updateSessionAgent: async (sessionId: string, agentId: string | null) => {
    const updatedSession = await updateSessionAgentApi(sessionId, agentId)

    updateSessionsCache((prev) =>
      prev.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              agentId: updatedSession.agentId,
              updatedAt: updatedSession.updatedAt,
            }
          : session,
      ),
    )

    // 同步刷新当前活跃会话主 Tab 的 agentId，使编辑保存后主 Tab 按钮立即
    // 显示新绑定的 Agent 名称（渲染层 ChatContainer 按 agentId 实时解析名称）。
    // 非当前活跃会话无需处理——下次进入会话时 initSessionTabs 会用最新
    // session.agentId 重建主 Tab。
    const agentTabStore = useAgentTabStore.getState()
    if (agentTabStore.currentSessionId === sessionId) {
      const mainTab = agentTabStore.tabs.find((t) => t.agentLevel === 1)
      if (mainTab) {
        agentTabStore.updateTab(mainTab.id, { agentId: updatedSession.agentId || undefined })
        agentTabStore.saveCurrentTabs()
      }
    }
  },

  toggleSessionStar: (sessionId: string) => {
    const session = readSessions().find((s) => s.id === sessionId)
    const newStarred = !session?.starred
    const prevStarred = session?.starred

    updateSessionsCache((prev) =>
      prev.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              starred: newStarred,
              updatedAt: new Date().toISOString(),
            }
          : session,
      ),
    )

    updateSessionApi(sessionId, {
      metadata: { starred: newStarred },
    }).catch((error) => {
      logger.error('星标同步失败:', error)
      updateSessionsCache((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, starred: prevStarred } : s)),
      )
      useNotificationStore.getState().addNotification({
        title: '操作同步失败',
        message: '星标状态同步失败，已恢复原状态',
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
        sourceLabel: '前端',
      })
    })
  },

  toggleSessionPin: (sessionId: string) => {
    const session = readSessions().find((s) => s.id === sessionId)
    const newPinned = !session?.pinned
    const prevPinned = session?.pinned

    updateSessionsCache((prev) =>
      prev.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              pinned: newPinned,
            }
          : session,
      ),
    )

    updateSessionApi(sessionId, {
      metadata: { pinned: newPinned },
    }).catch((error) => {
      logger.error('置顶同步失败:', error)
      updateSessionsCache((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, pinned: prevPinned } : s)),
      )
      useNotificationStore.getState().addNotification({
        title: '操作同步失败',
        message: '置顶状态同步失败，已恢复原状态',
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
        sourceLabel: '前端',
      })
    })
  },

  renameSession: async (sessionId: string, newTitle: string) => {
    if (!newTitle.trim()) {
      return
    }
    const trimmedTitle = newTitle.trim()
    const session = readSessions().find((s) => s.id === sessionId)
    const prevTitle = session?.title
    const prevUpdatedAt = session?.updatedAt

    updateSessionsCache((prev) =>
      prev.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              title: trimmedTitle,
              updatedAt: new Date().toISOString(),
            }
          : session,
      ),
    )
    try {
      await updateSessionApi(sessionId, { title: trimmedTitle })
    } catch (error) {
      logger.error('重命名会话失败:', error)
      updateSessionsCache((prev) =>
        prev.map((s) =>
          s.id === sessionId ? { ...s, title: prevTitle ?? s.title, updatedAt: prevUpdatedAt ?? s.updatedAt } : s,
        ),
      )
      useNotificationStore.getState().addNotification({
        title: '操作同步失败',
        message: '重命名同步失败，已恢复原标题',
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
        sourceLabel: '前端',
      })
    }
  },

  searchSessions: (keyword: string) => {
    const sessions = readSessions()

    if (!keyword.trim()) {
      return sessions
    }

    const lowerKeyword = keyword.toLowerCase()
    return sessions
      .filter((session) => session.title.toLowerCase().includes(lowerKeyword))
      .sort((a, b) => {
        if (a.pinned !== b.pinned) {
          return a.pinned ? -1 : 1
        }
        return (
          new Date(b.updatedAt || b.createdAt).getTime() -
          new Date(a.updatedAt || a.createdAt).getTime()
        )
      })
  },

  copySession: async (sessionId: string) => {
    const session = readSessions().find((s) => s.id === sessionId)
    if (!session) {
      throw new Error('会话不存在')
    }

    const newTitle = `${session.title} (副本)`
    const newSession = await get().createSession(newTitle, {
      agentId: session.agentId || undefined,
    })

    return newSession
  },

  /** 首次 AI 回复完成后，根据首条用户消息自动重命名会话。 条件：会话标题仍为默认值（generateSessionTitle 返回的值）时才触发， */
  autoRenameSessionIfNeeded: (sessionId: string, pipelineId: string) => {
    const session = readSessions().find((s) => s.id === sessionId)
    if (!session) return

    // 仅当标题仍为默认值时才自动重命名
    if (session.title !== DEFAULT_AGENT_NAME) return

    const pipelineStore = usePipelineMessageStore.getState()
    const messages = pipelineStore.getMessages(pipelineId)
    if (!messages || messages.length === 0) return

    // 找到第一条 role=user 的消息
    const firstUserMsg = messages.find(
      (m: import('@/types/models').Message) => m.role === 'user',
    )
    if (!firstUserMsg) return

    // 从 parts 中提取文本内容，优先使用 parts；fallback 到 content 字段
    let userText = ''
    if (firstUserMsg.parts && firstUserMsg.parts.length > 0) {
      const textParts = firstUserMsg.parts.filter(
        (p: import('@/types/messageParts').MessagePart) => p.type === 'text',
      )
      userText = textParts.map((p: any) => p.content || '').join('').trim()
    }
    if (!userText) {
      userText = (firstUserMsg.content || '').trim()
    }
    if (!userText) return

    // 截取前 30 个字符，避免标题过长
    const maxTitleLength = 30
    let title = userText.replace(/\n/g, ' ').trim()
    if (title.length > maxTitleLength) {
      title = title.slice(0, maxTitleLength) + '…'
    }
    if (!title) return

    get().renameSession(sessionId, title)
  },
}))
