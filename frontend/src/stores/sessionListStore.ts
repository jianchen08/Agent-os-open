/** 会话列表状态管理 Store */

import { create } from 'zustand'
import {
  createSession as createSessionApi,
  deleteSession as deleteSessionApi,
  getSessions,
  updateSessionAgent as updateSessionAgentApi,
  updateSession as updateSessionApi,
} from '@/services/api/session'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { loggers } from '@/utils/logger'
import { uiStorage, STORAGE_KEYS } from '@/utils/storage'
import { useAgentStore } from './agentStore'
import { useAgentTabStore } from './agentTabStore'
import { useLayoutModeStore } from './layoutModeStore'
import { useNotificationStore } from './notificationStore'
import { usePipelineMessageStore } from './pipelineMessageStore'
import { useSessionStore } from './sessionStore'
import type { Session } from '@/types/models'

const logger = loggers.sessionStore

interface CreateSessionOptions {
  agentId?: string
}

interface SessionListState {
  fetchSessions: (options?: { background?: boolean }) => Promise<void>
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

export const useSessionListStore = create<SessionListState>()((set, get) => ({
  fetchSessions: async (options?: { background?: boolean }) => {
      const sessionStore = useSessionStore.getState()
      if (sessionStore.isLoading) {
        return
      }

      const isBackground = options?.background ?? false
      const hadNoActiveSession = !sessionStore.activeSessionId

      if (!isBackground) {
        useSessionStore.setState({ isLoading: true, error: null })
      }

      try {
        const sessions = await getSessions()
        const validSessionIds = new Set(sessions.map((s) => s.id))

        useSessionStore.setState((state) => {
          let newActiveSessionId: string | null = null

          if (state.activeSessionId && validSessionIds.has(state.activeSessionId)) {
            newActiveSessionId = state.activeSessionId
          } else if (hadNoActiveSession) {
            // 从 localStorage 恢复上次选中的会话
            const savedSessionId = uiStorage.getLastActiveSession()
            if (savedSessionId && validSessionIds.has(savedSessionId)) {
              newActiveSessionId = savedSessionId
            }
          }

          return {
            sessions: sessions,
            activeSessionId: newActiveSessionId,
            isLoading: false,
            error: null,
          }
        })

        // 从 localStorage 恢复会话后触发完整的数据加载
        if (hadNoActiveSession) {
          const restoredId = useSessionStore.getState().activeSessionId
          if (restoredId) {
            await get().setActiveSession(restoredId)
          }
        }
      } catch (error: any) {
        const errorMessage = error.message || '获取会话列表失败'
        useSessionStore.setState({ isLoading: false, error: errorMessage })
        throw new Error(errorMessage)
      }
  },

  createSession: async (title?: string, options?: CreateSessionOptions) => {
    useSessionStore.setState({ isLoading: true, error: null })

    try {
      const sessionTitle = title || generateSessionTitle()

      const newSession = await createSessionApi({
        title: sessionTitle,
        agentId: options?.agentId,
      })

      useSessionStore.setState((state) => ({
        sessions: [...state.sessions, newSession],
        activeSessionId: newSession.id,
        isLoading: false,
        error: null,
      }))

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
    } catch (error: any) {
      const errorMessage = error.message || '创建会话失败'
      useSessionStore.setState({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /** 删除会话（含完整清理） */
  deleteSession: async (id: string) => {
    useSessionStore.setState((state) => ({
      deletingSessionIds: new Set(state.deletingSessionIds).add(id),
      error: null,
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
        isLoadingOlderByPipeline: curLoadingOlder,
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
        isLoadingOlderByPipeline: filterByKey(curLoadingOlder),
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

      // 7. 更新 sessionStore 状态
      useSessionStore.setState((state) => {
        const newDeletingIds = new Set(state.deletingSessionIds)
        newDeletingIds.delete(id)

        const safePagination = state.messagePagination || {}
        const { [id]: _removedPagination, ...restPagination } = safePagination

        return {
          sessions: state.sessions.filter((session) => session.id !== id),
          activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
          deletingSessionIds: newDeletingIds,
          messagePagination: restPagination,
          error: null,
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
        return { deletingSessionIds: newDeletingIds, error: errorMessage }
      })
      throw new Error(errorMessage)
    }
  },

  setActiveSession: async (id: string, fetchData: boolean = true) => {
    if (!id || id.trim().length === 0) {
      return
    }

    const sessions = useSessionStore.getState().sessions
    const sessionExists = sessions.some((s) => s.id === id)
    if (!sessionExists) {
      return
    }

    // s.activePipelineId，显示"老数据"（上一个会话的消息）。
    useAgentTabStore.getState().initSessionTabs(id)

    useSessionStore.setState({ activeSessionId: id })
    // 持久化当前活跃会话ID，页面刷新后可恢复
    uiStorage.setLastActiveSession(id)

    const session = sessions.find((s) => s.id === id)
    if (session?.agentId) {
      const agents = useAgentStore.getState().agents
      const matchedAgent = agents.find(
        (a) => a.id === session.agentId || a.configId === session.agentId,
      )
      if (matchedAgent) {
        useAgentStore.getState().setCurrentAgentId(matchedAgent.id)
      }
    }

    if (fetchData) {
      try {
        const pipelineId = session?.pipelineIds?.[0]
        if (!pipelineId) {
          console.error('[setActiveSession] 会话缺少主管道: sessionId=%s pipelineIds=%o', id, session?.pipelineIds)
        }
        if (pipelineId) {
          // 统一加载入口：流式保护 + 双游标决策（init/after_sequence）已收敛到
          // loadPipelineMessages 内部，会话切换走默认 mode='auto'。
          await usePipelineMessageStore.getState().loadPipelineMessages(pipelineId, { threadId: id })
        }
      } catch (error) {
        console.error('[setActiveSession] 加载会话数据失败:', error)
      }

      // 只有提交任务后 useRealtimeEvents 中才会触发刷新。
      useLayoutModeStore.getState().bumpWorkspaceDataVersion()
    }
  },

  updateSession: (sessionId: string, updates: Partial<Session>) => {
    useSessionStore.setState((state) => ({
      sessions: state.sessions.map((session) =>
        session.id === sessionId
          ? { ...session, ...updates, updatedAt: new Date().toISOString() }
          : session,
      ),
    }))
  },

  updateSessionAgent: async (sessionId: string, agentId: string | null) => {
    useSessionStore.setState({ isLoading: true, error: null })

    try {
      const updatedSession = await updateSessionAgentApi(sessionId, agentId)

      useSessionStore.setState((state) => ({
        sessions: state.sessions.map((session) =>
          session.id === sessionId
            ? {
                ...session,
                agentId: updatedSession.agentId,
                updatedAt: updatedSession.updatedAt,
              }
            : session,
        ),
        isLoading: false,
        error: null,
      }))

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
    } catch (error: any) {
      const errorMessage = error.message || '更新会话 Agent 绑定失败'
      useSessionStore.setState({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  toggleSessionStar: (sessionId: string) => {
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
    const newStarred = !session?.starred
    const prevStarred = session?.starred

    useSessionStore.setState((state) => ({
      sessions: state.sessions.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              starred: newStarred,
              updatedAt: new Date().toISOString(),
            }
          : session,
      ),
    }))

    updateSessionApi(sessionId, {
      metadata: { starred: newStarred },
    }).catch((error) => {
      logger.error('星标同步失败:', error)
      useSessionStore.setState((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId ? { ...s, starred: prevStarred } : s,
        ),
      }))
      useNotificationStore.getState().addNotification({
        title: '操作同步失败',
        message: '星标状态同步失败，已恢复原状态',
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
      })
    })
  },

  toggleSessionPin: (sessionId: string) => {
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
    const newPinned = !session?.pinned
    const prevPinned = session?.pinned

    useSessionStore.setState((state) => ({
      sessions: state.sessions.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              pinned: newPinned,
            }
          : session,
      ),
    }))

    updateSessionApi(sessionId, {
      metadata: { pinned: newPinned },
    }).catch((error) => {
      logger.error('置顶同步失败:', error)
      useSessionStore.setState((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId ? { ...s, pinned: prevPinned } : s,
        ),
      }))
      useNotificationStore.getState().addNotification({
        title: '操作同步失败',
        message: '置顶状态同步失败，已恢复原状态',
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
      })
    })
  },

  renameSession: async (sessionId: string, newTitle: string) => {
    if (!newTitle.trim()) {
      return
    }
    const trimmedTitle = newTitle.trim()
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
    const prevTitle = session?.title
    const prevUpdatedAt = session?.updatedAt

    useSessionStore.setState((state) => ({
      sessions: state.sessions.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              title: trimmedTitle,
              updatedAt: new Date().toISOString(),
            }
          : session,
      ),
    }))
    try {
      await updateSessionApi(sessionId, { title: trimmedTitle })
    } catch (error) {
      logger.error('重命名会话失败:', error)
      useSessionStore.setState((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? { ...s, title: prevTitle, updatedAt: prevUpdatedAt }
            : s,
        ),
      }))
      useNotificationStore.getState().addNotification({
        title: '操作同步失败',
        message: '重命名同步失败，已恢复原标题',
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
      })
    }
  },

  searchSessions: (keyword: string) => {
    const sessions = useSessionStore.getState().sessions

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
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
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
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
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
