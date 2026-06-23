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
            // BUG-FIX-fix_20260528_session_persist: 从 localStorage 恢复上次选中的会话
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

        // BUG-FIX-fix_20260528_session_persist: 从 localStorage 恢复会话后触发完整的数据加载
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

      // BUG-FIX-fix_20260528_session_persist: 删除当前活跃会话时清理持久化的会话ID
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

    // BUG-FIX-fix_20260604_stale_pipeline_id:
    // 问题根因: setState 设 activeSessionId 触发 ChatContainer 渲染，
    //          但 activatePipeline 还没跑，selector 短暂读到上一个会话的
    //          s.activePipelineId，显示"老数据"（上一个会话的消息）。
    // 修复方案: 在 setState 之前先调 initSessionTabs 同步激活管道，
    //          确保 selector 首次渲染就读到正确的 activePipelineId。
    // 影响范围: 会话切换时的消息显示时序
    // 修复日期: 2026-06-04
    useAgentTabStore.getState().initSessionTabs(id)

    useSessionStore.setState({ activeSessionId: id })
    // BUG-FIX-fix_20260528_session_persist: 持久化当前活跃会话ID，页面刷新后可恢复
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
        // BUG-FIX-fix_20260617_remove_main_pipeline_fallback:
        // 问题根因: 原代码用 session.activePipelineId 作为主管道 fallback，
        //          但 activePipelineId 在派生过子 Tab 时会指向子管道，作为主管道兜底是语义错误，
        //          会导致 fetchMessages 用错误的 pipelineId 加载消息。
        // 修复方案: 只用 session.pipelineIds[0] 作为主管道，缺失时记 error 并跳过加载。
        // 影响范围: 会话切换时的消息加载
        // 修复日期: 2026-06-17
        const pipelineId = session?.pipelineIds?.[0]
        if (!pipelineId) {
          console.error('[setActiveSession] 会话缺少主管道: sessionId=%s pipelineIds=%o', id, session?.pipelineIds)
        }
        if (pipelineId) {
          // BUG-FIX-fix_20260515_streaming_interrupt:
          // 问题根因: 切换回正在流式输出的会话时，fetchMessages -> initFromAPI
          //          会用后端 API 数据覆盖本地的流式消息，导致流式输出中断。
          // 修复方案: 管道正在流式传输时跳过 API 请求，保留本地流式数据。
          // 影响范围: 会话切换时的流式输出连续性
          // 修复日期: 2026-05-15
          //
          // BUG-FIX-fix_20260528_refresh_streaming_only_one_msg:
          // 问题根因: 页面刷新后 WS 重连，后端继续发送流式事件（stream_start），
          //          在用户点击会话之前就创建了流式占位消息并设置 streamingState。
          //          用户点击时 isStreaming 返回 true，fetchMessages 被跳过，
          //          历史消息未加载，只显示一条流式占位消息。
          // 修复方案: 即使管道正在流式传输，如果本地消息数量极少（<=1，仅占位消息），
          //          仍然调用 fetchMessages 加载历史消息，initFromAPI 的合并逻辑会保留流式消息。
          // 影响范围: 页面刷新后进入正在输出的会话时的消息显示
          // 修复日期: 2026-05-28
          const pipelineStore = usePipelineMessageStore.getState()
          const existingCount = (pipelineStore.messagesByPipeline[pipelineId] || []).length
          if (!pipelineStore.isStreaming(pipelineId) || existingCount <= 1) {
            await pipelineStore.fetchMessages(pipelineId, { threadId: id })
          }
        }
      } catch (error) {
        console.error('[setActiveSession] 加载会话数据失败:', error)
      }

      // BUG-FIX-fix_20260521_tasklist_refresh:
      // 问题根因: 切换会话时未调用 bumpWorkspaceDataVersion()，导致工作区组件
      //          （包括 FileTreeWidget 渲染的任务列表）不会重新加载数据。
      //          只有提交任务后 useRealtimeEvents 中才会触发刷新。
      // 修复方案: 会话切换并加载数据后，主动触发工作区数据版本递增，通知所有依赖
      //          workspaceRefreshKey 的组件重新加载对应会话的数据。
      // 影响范围: 会话切换时的工作区（任务列表、文件树等）刷新行为
      // 修复日期: 2026-05-21
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

    // BUG-FIX-fix_20260617_optimistic_no_rollback:
    // 问题根因: 原代码同步失败仅 logger.error，不回滚 UI 不告知用户，
    //          导致 UI 显示与后端不一致，用户误以为操作成功。
    // 修复方案: 失败时回滚乐观更新（恢复原值）并通过通知告知用户。
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

    // BUG-FIX-fix_20260617_optimistic_no_rollback:
    // 问题根因: 原代码同步失败仅 logger.error，不回滚 UI 不告知用户。
    // 修复方案: 失败时回滚乐观更新（恢复原值）并通过通知告知用户。
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
      // BUG-FIX-fix_20260617_optimistic_no_rollback:
      // 问题根因: 原代码 catch 仅 logger.error，不回滚 UI 不告知用户，
      //          导致 UI 显示新标题但后端仍是旧标题。
      // 修复方案: 失败时回滚乐观更新（恢复原标题）并通过通知告知用户。
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

  /**
   * 首次 AI 回复完成后，根据首条用户消息自动重命名会话。
   *
   * 条件：会话标题仍为默认值（generateSessionTitle 返回的值）时才触发，
   * 用户手动重命名过的会话不会被覆盖。
   */
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
