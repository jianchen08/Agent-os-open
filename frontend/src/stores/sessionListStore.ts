/**
 * 会话列表状态管理 Store
 */

import { create } from 'zustand'
import {
  createSession as createSessionApi,
  deleteSession as deleteSessionApi,
  getSessions,
  updateSessionAgent as updateSessionAgentApi,
  updateSession as updateSessionApi,
} from '@/services/api/session'
import { loggers } from '@/utils/logger'
import { useAgentStore } from './agentStore'
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
}

/** 默认主 Agent 名称 */
const DEFAULT_AGENT_NAME = '灵汐'

/**
 * 生成默认会话标题，使用主 Agent 名称。
 */
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

    if (!isBackground) {
      useSessionStore.setState({ isLoading: true, error: null })
    }

    try {
      const sessions = await getSessions()
      const validSessionIds = new Set(sessions.map((s) => s.id))

      useSessionStore.setState((state) => {
        const activeSessionExistsInBackend = state.activeSessionId
          ? validSessionIds.has(state.activeSessionId)
          : false

        const newActiveSessionId = activeSessionExistsInBackend ? state.activeSessionId : null

        return {
          sessions: sessions,
          activeSessionId: newActiveSessionId,
          isLoading: false,
          error: null,
        }
      })
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

      return newSession
    } catch (error: any) {
      const errorMessage = error.message || '创建会话失败'
      useSessionStore.setState({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  deleteSession: async (id: string) => {
    useSessionStore.setState((state) => ({
      deletingSessionIds: new Set(state.deletingSessionIds).add(id),
      error: null,
    }))

    try {
      await deleteSessionApi(id)

      useSessionStore.setState((state) => {
        const newDeletingIds = new Set(state.deletingSessionIds)
        newDeletingIds.delete(id)

        const { [id]: _removedMessages, ...restMessages } = state.messages
        const { [id]: _removedPagination, ...restPagination } = state.messagePagination

        return {
          sessions: state.sessions.filter((session) => session.id !== id),
          activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
          deletingSessionIds: newDeletingIds,
          messages: restMessages,
          messagePagination: restPagination,
          error: null,
        }
      })
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

    useSessionStore.setState({ activeSessionId: id })

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
        await useSessionStore.getState().fetchMessages(id)
      } catch (error) {
        console.error('[setActiveSession] 加载会话数据失败:', error)
      }
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
    } catch (error: any) {
      const errorMessage = error.message || '更新会话 Agent 绑定失败'
      useSessionStore.setState({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  toggleSessionStar: (sessionId: string) => {
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
    const newStarred = !session?.starred

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

    // 持久化到后端 metadata
    updateSessionApi(sessionId, {
      metadata: { starred: newStarred },
    }).catch((error) => {
      logger.error('星标同步失败:', error)
    })
  },

  toggleSessionPin: (sessionId: string) => {
    const session = useSessionStore.getState().sessions.find((s) => s.id === sessionId)
    const newPinned = !session?.pinned

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

    // 持久化到后端 metadata
    updateSessionApi(sessionId, {
      metadata: { pinned: newPinned },
    }).catch((error) => {
      logger.error('置顶同步失败:', error)
    })
  },

  renameSession: async (sessionId: string, newTitle: string) => {
    if (!newTitle.trim()) {
      return
    }
    useSessionStore.setState((state) => ({
      sessions: state.sessions.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              title: newTitle.trim(),
              updatedAt: new Date().toISOString(),
            }
          : session,
      ),
    }))
    try {
      await updateSessionApi(sessionId, { title: newTitle.trim() })
    } catch (error) {
      logger.error('重命名会话失败:', error)
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
}))
