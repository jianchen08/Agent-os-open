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
import { useAgentTabStore } from './agentTabStore'
import { usePipelineMessageStore } from './pipelineMessageStore'
import { useSessionStore } from './sessionStore'
import { useStreamingStore } from './streamingStore'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
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

  /**
   * 删除会话（含完整清理）
   *
   * BUG-FIX-fix_20260513_delete_session:
   * 问题根因: 删除会话时仅按 sessionId 清理，未遍历 pipelineSessionMap 找到所有子管道，
   *          导致流式传输未终止、子管道数据残留、Agent Tab 状态未清理、后端进程未取消。
   * 修复方案: 按顺序执行：发送取消信号 → 停止流式 → 查找所有管道 → 清理管道数据 →
   *          清理 Agent Tab → 调用后端 API → 更新会话状态。
   * 影响范围: 会话删除流程的所有 Store 状态
   * 修复日期: 2026-05-13
   */
  deleteSession: async (id: string) => {
    useSessionStore.setState((state) => ({
      deletingSessionIds: new Set(state.deletingSessionIds).add(id),
      error: null,
    }))

    try {
      // 1. 发送 WebSocket 取消信号，让后端停止运行中的 Agent 进程
      globalWS.sendCancel(id, '会话已删除')

      // 2. 查找所有属于该会话的 pipelineId（主管道 + 子管道 + 孙管道）
      const pipelineStore = usePipelineMessageStore.getState()
      const allPipelineIds = Object.entries(pipelineStore.pipelineSessionMap)
        .filter(([, sessionId]) => sessionId === id)
        .map(([pipelineId]) => pipelineId)
      // sessionId 本身也可能是主管道 ID
      if (!allPipelineIds.includes(id)) {
        allPipelineIds.push(id)
      }

      // 3. 停止所有管道的流式传输
      const streamingStore = useStreamingStore.getState()
      for (const pipelineId of allPipelineIds) {
        const tabId = useAgentTabStore.getState().getTabIdByPipeline(pipelineId)
        if (tabId) {
          streamingStore.stopStreamingForTab(tabId)
        }
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

        const { [id]: _removedPagination, ...restPagination } = state.messagePagination

        return {
          sessions: state.sessions.filter((session) => session.id !== id),
          activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
          deletingSessionIds: newDeletingIds,
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
        const pipelineId = session?.activePipelineId || session?.pipelineIds?.[0]
        if (pipelineId) {
          await usePipelineMessageStore.getState().fetchMessages(pipelineId, { threadId: id })
        }
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
