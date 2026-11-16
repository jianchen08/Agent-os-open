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
import { readAgents } from '@/hooks/queries/useAgentsQuery'
import { readSessions, updateSessionsCache } from '@/hooks/queries/useSessionsQuery'
import {
  createSession as createSessionApi,
  deleteSession as deleteSessionApi,
  updateSessionAgent as updateSessionAgentApi,
  updateSession as updateSessionApi,
} from '@/services/api/session'
import { clearSessionExecutionOptions } from '@/services/sessionExecutionOptions'
import { forgetSession, rememberSession } from '@/services/sessionWatchlist'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { loggers } from '@/utils/logger'
import { mainPipelineIdOf } from '@/utils/mappers'
import { uiStorage, STORAGE_KEYS } from '@/utils/storage'
import { userScopedKey } from '@/utils/userScopedStorage'
import { useAgentStore } from './agentStore'
import { useAgentTabStore } from './agentTabStore'
import { useNotificationStore } from './notificationStore'
import { usePipelineMessageStore } from './pipelineMessageStore'
import { useSessionStore } from './sessionStore'
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
  /**
   * 冷进入直载已尝试（R10② 一次语义锚，2026-10-14 用户裁定）：进入应用若有
   * 会话直接加载最近会话、不停在欢迎页，但**只发生一次**——此后删会话/后台
   * 刷新回到无选中态时不再自动抓会话，用户主动回欢迎页的路径不被劫持。
   */
  coldStartRestoreDone: boolean
  /** query 数据到位后恢复上次选中会话（幂等：已有有效选中时不动作） */
  restoreActiveSessionIfNeeded: (sessions: Session[]) => Promise<void>
  createSession: (title?: string, options?: CreateSessionOptions) => Promise<Session>
  deleteSession: (id: string) => Promise<void>
  setActiveSession: (id: string, fetchData?: boolean) => Promise<void>
  updateSession: (sessionId: string, updates: Partial<Session>) => void
  /**
   * 编辑保存：工作空间字段持久化到服务端 thread metadata（PATCH 按 key 合并，
   * 不触碰 pinned/starred 等既有键）并同步本地缓存——会话树聚合与列表徽标以
   * session.workspace 为真值源，只存本地执行快照不会聚合。
   */
  updateSessionWorkspace: (
    sessionId: string,
    workspace: string,
    workspaceMode?: 'plain' | 'worktree',
  ) => Promise<void>
  toggleSessionPin: (sessionId: string) => void
  updateSessionAgent: (sessionId: string, agentId: string | null) => Promise<void>
  toggleSessionStar: (sessionId: string) => void
  renameSession: (sessionId: string, newTitle: string) => void
  searchSessions: (keyword: string) => Session[]
  copySession: (sessionId: string) => Promise<Session>
  /**
   * 从 thread 重建会话条目（会话记录缺失补救面）：线程记录已从内核删除（如
   * 用户清空会话数据）而消息仍按 thread_id 可读时，会话归属链断裂导致任务
   * 行/管道入口点开静默无反应——按已知线索合成最小会话条目写入缓存，后续
   * setActiveSession 以其 pipelineIds[0] 为主管道真值正常打开对话。同 id
   * 条目已存在（真实记录或先前重建）时跳过：幂等，且真实记录不被合成值覆盖。
   */
  rebuildSessionFromThread: (
    threadId: string,
    hints?: { title?: string; pipelineId?: string },
  ) => void
  /** 首次 AI 回复完成后，根据首条用户消息自动重命名会话 */
  autoRenameSessionIfNeeded: (sessionId: string, pipelineId: string) => void
}

/** 默认主 Agent 名称 */
const DEFAULT_AGENT_NAME = '灵汐'

/** 会话最近活跃时间戳（updatedAt 优先，createdAt 兜底；非法值按 0=最旧） */
export function sessionRecencyMs(session: Session): number {
  const ms = new Date(session.updatedAt || session.createdAt).getTime()
  return Number.isNaN(ms) ? 0 : ms
}

/** 最近活跃比较器（降序）：新在前，旧在后 */
export function compareSessionRecency(a: Session, b: Session): number {
  return sessionRecencyMs(b) - sessionRecencyMs(a)
}

/**
 * 会话按最近活跃降序（R10 多端统一单点排序，2026-10-14 用户裁定）：
 * 最近使用的在前；星标只作行内视觉标记（图标），不参与排序。侧栏列表、
 * 搜索结果、直载最近会话全部经本函数同序——桌面与移动端共用此前端层，
 * 天然一致。2026-11-06 用户裁定侧栏列表在本序之上叠加置顶组浮顶
 * （SessionList 内摘出置顶单独成组置于顶端，组内仍用本序），本函数
 * 语义不变（组内成员、搜索、直载仍纯最近活跃）。返回新数组不 mutate 入参。
 */
export function sortSessionsByRecency(sessions: Session[]): Session[] {
  return [...sessions].sort(compareSessionRecency)
}

/** 生成默认会话标题，使用主 Agent 名称。 */
const generateSessionTitle = (): string => {
  return DEFAULT_AGENT_NAME
}

export const useSessionListStore = create<SessionListState>()((_, get) => ({
  coldStartRestoreDone: false,

  restoreActiveSessionIfNeeded: async (sessions: Session[]) => {
    // R10②：冷进入直载只发生一次（首次数据到位即消耗机会），此后不再自动选中
    if (get().coldStartRestoreDone) {
      return
    }
    useSessionListStore.setState({ coldStartRestoreDone: true })
    const validSessionIds = new Set(sessions.map((s) => s.id))
    const currentActive = useSessionStore.getState().activeSessionId
    // 幂等保护：已有有效选中（含后台刷新重跑本函数）直接返回
    if (currentActive && validSessionIds.has(currentActive)) {
      return
    }
    // 优先复用既有 last_active_session 持久化（语义匹配：用户最后所在=最近使用）
    const savedSessionId = uiStorage.getLastActiveSession()
    if (savedSessionId && validSessionIds.has(savedSessionId)) {
      await get().setActiveSession(savedSessionId)
      return
    }
    // 无持久化选中（或已失效）→ 直接加载列表顶部会话（R10① 排序后第一个=
    // 最近使用），不停在欢迎页；无会话保持欢迎页。加载失败经调用方传播、
    // 选中态保持空 → 主界面自然回落欢迎页（错误是值，可恢复）。
    const latest = sortSessionsByRecency(sessions)[0]
    if (latest) {
      await get().setActiveSession(latest.id)
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
    // 登记进本地登记表：服务端列表按 updated_at DESC 截断 100 条，零活动新会话
    // 会被活跃会话挤出窗口——登记后每次列表拉取合并补回（刷新后仍可见）
    rememberSession(newSession)
    useSessionStore.setState({ activeSessionId: newSession.id })

    // 会话面初始化与 setActiveSession 同款：重建 agentTabStore 标签面（顶部
    // 对话标签随 activeTabId 切换、ChatInput 随 key 重建草稿状态）+ 持久化
    // 选中会话。缺失会导致创建会话后消息区已切到新管道、但顶部标签与输入框
    // 状态仍停留上一会话（需手动切换才对齐）。
    useAgentTabStore.getState().initSessionTabs(newSession.id)
    uiStorage.setLastActiveSession(newSession.id)

    const mainPid = mainPipelineIdOf(newSession)
    if (mainPid) {
      const pipelineStore = usePipelineMessageStore.getState()
      pipelineStore.registerPipeline({
        pipelineId: mainPid,
        sessionId: newSession.id,
        level: 1,
        tabId: null,
        agentName: '',
        status: 'idle',
        parentId: null,
        unreadCount: 0,
      })
      pipelineStore.activatePipeline(mainPid)
      logger.info(
        '[createSession] pipeline registered: sessionId=%s pipelineId=%s',
        newSession.id.slice(0, 12),
        mainPid.slice(0, 12),
      )
    }

    return newSession
  },

  /** 删除会话（含完整清理）：先 API 后清本地——API 失败时本地数据原封不动，可重试 */
  deleteSession: async (id: string) => {
    useSessionStore.setState((state) => ({
      deletingSessionIds: new Set(state.deletingSessionIds).add(id),
    }))

    try {
      // 1. 后端删除先行：失败（网络/5xx）在此拒绝，本地消息/标签页/快照一律
      // 未动，提示后用户可直接重试
      await deleteSessionApi(id)
    } catch (error: any) {
      const errorMessage = error.message || '删除会话失败'
      useSessionStore.setState((state) => {
        const newDeletingIds = new Set(state.deletingSessionIds)
        newDeletingIds.delete(id)
        return { deletingSessionIds: newDeletingIds }
      })
      useNotificationStore.getState().addNotification({
        title: '删除失败，数据未变',
        message: errorMessage,
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 5000,
        sourceLabel: '前端',
      })
      throw new Error(errorMessage)
    }

    // 2. 删除已成立才清理本地。取消信号也移到成功后：删除未成立时不需要
    // 取消在途流，信号移后语义不变。
    const pipelineStore = usePipelineMessageStore.getState()
    const allPipelineIds = Object.entries(pipelineStore.pipelineSessionMap)
      .filter(([, sessionId]) => sessionId === id)
      .map(([pipelineId]) => pipelineId)
    for (const pid of allPipelineIds) {
      globalWS.sendCancel(id, '会话已删除', pid)
    }

    // 3. 查找所有属于该会话的 pipelineId（主管道 + 子管道 + 孙管道）
    // sessionId 本身也可能是主管道 ID
    if (!allPipelineIds.includes(id)) {
      allPipelineIds.push(id)
    }

    // 4. 停止所有管道的流式传输
    for (const pipelineId of allPipelineIds) {
      pipelineStore.stopStreaming(pipelineId)
    }

    // 5. 清理 pipelineMessageStore 中所有相关管道的数据
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

    // 6. 清理 agentTabStore（标签页、映射、localStorage）
    const agentTabStore = useAgentTabStore.getState()
    if (agentTabStore.currentSessionId === id) {
      agentTabStore.resetAllTabs()
      try {
        localStorage.removeItem(`agent-tabs-${id}`)
      } catch {
        // localStorage 清理失败不影响主流程
      }
    }

    // 执行选项本地快照随会话删除（残留键会被同名会话覆写，但显式清理更干净）
    clearSessionExecutionOptions(id)

    // 7. 更新缓存与会话选中态（注销登记表：删除后不得被合并复活）
    forgetSession(id)
    updateSessionsCache((prev) => prev.filter((session) => session.id !== id))
    useSessionStore.setState((state) => {
      const newDeletingIds = new Set(state.deletingSessionIds)
      newDeletingIds.delete(id)

      return {
        activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
        deletingSessionIds: newDeletingIds,
      }
    })

    // X18-F1（P-139）：通知中心里带该 sessionId 的条目一并摘除（残留只能
    // 点击空路由，留驻到刷新）
    useNotificationStore.getState().pruneForTarget({ sessionId: id })

    // 删除当前活跃会话时清理持久化的会话ID
    if (!useSessionStore.getState().activeSessionId) {
      // 键经 uiStorage 同源的账户命名空间（隔离执法点①），裸键删除会漏 scoped 键
      try { localStorage.removeItem(userScopedKey(STORAGE_KEYS.LAST_ACTIVE_SESSION)) } catch (_e) { /* localStorage 清理失败不影响主流程 */ }
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

    // initSessionTabs 内部先清 pipelineMessageStore.activePipelineId（跨会话
    // 单例的视图指针）再重建标签面——不清则切换会话后显示"老数据"（上一会话的消息）。
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
        // 主管道解析：映射真值 pipelineIds[0]（与任务管理面板同源，见 mainPipelineIdOf）
        const pipelineId = mainPipelineIdOf(session)
        if (!pipelineId) {
          console.error('[setActiveSession] 会话缺少主 pipeline: sessionId=%s pipelineIds=%o', id, session.pipelineIds)
        }
        if (pipelineId) {
          // 统一加载入口：流式保护 + 双游标决策（init/after_sequence）已收敛到
          // loadPipelineMessages 内部，会话切换走默认 mode='auto'。
          const result = await usePipelineMessageStore.getState().loadPipelineMessages(pipelineId, { threadId: id })
          if (!result.ok) {
            // X1-F1：加载失败必须给用户可见反馈——落该管道失败态，消息区据此
            // 渲染错误提示 + 「重新加载」（替代误导性空态），自愈通道不变（重试/
            // 下次进入/WS 重连对账成功时标记由 loadPipelineMessages 清除）。
            console.error('[setActiveSession] 加载会话数据失败:', result.error)
            usePipelineMessageStore.getState().setLoadError(pipelineId, true)
          }
        }
      } catch (error) {
        // 防御兜底：loadPipelineMessages 契约不抛（失败转 {ok:false} 由上方分支
        // 通知用户），此 catch 仅接加载链路外的非常规异常，记录后不阻断切换。
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

  updateSessionWorkspace: async (sessionId, workspace, workspaceMode) => {
    const metadata: Record<string, unknown> = { workspace }
    if (workspaceMode) {
      metadata.workspace_mode = workspaceMode
    }
    // 服务端先行为真（PATCH 按 key 合并）；失败在此拒绝、缓存未动，可重试
    await updateSessionApi(sessionId, { metadata })
    updateSessionsCache((prev) =>
      prev.map((session) =>
        session.id === sessionId
          ? {
              ...session,
              workspace,
              ...(workspaceMode ? { workspaceMode } : {}),
              updatedAt: new Date().toISOString(),
            }
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
      // 主标签名/面包屑随会话标题走（ADR 2026-10-16-subagent-tab-naming）：
      // 改名成功即同步已开的主标签（自动改名也经本函数，单点覆盖）
      useAgentTabStore.getState().updateTab(`main-${sessionId}`, {
        agentName: trimmedTitle,
        path: [trimmedTitle],
      })
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
    // 与侧栏同序（R10 单点排序）：过滤后按最近活跃降序，置顶不参与
    return sortSessionsByRecency(
      sessions.filter((session) => session.title.toLowerCase().includes(lowerKeyword)),
    )
  },

  rebuildSessionFromThread: (threadId, hints) => {
    if (readSessions().some((s) => s.id === threadId)) return
    // 时间戳取重建时刻：线程记录已删，创建/更新时间客户端侧无真值可取
    // （无按 id 读端点）；侧栏按 updatedAt 排序时重建条目最近可辨。
    const nowIso = new Date().toISOString()
    updateSessionsCache((prev) => [
      ...prev,
      {
        id: threadId,
        title: hints?.title || '未命名会话',
        createdAt: nowIso,
        updatedAt: nowIso,
        messageCount: 0,
        pipelineIds: hints?.pipelineId ? [hints.pipelineId] : [],
      },
    ])
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
