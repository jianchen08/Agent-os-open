/**
 * sessionListStore 单元测试
 *
 * 验证会话列表管理 Store 的核心公共接口：
 * - searchSessions 关键词搜索与排序
 * - updateSession 本地状态更新
 * - toggleSessionPin / toggleSessionStar 置顶与星标切换
 * - renameSession 重命名（空标题忽略）
 * - autoRenameSessionIfNeeded 自动重命名逻辑
 * - setActiveSession 空ID与不存在ID的防护
 * - copySession 正常复制与不存在会话错误
 * - deleteSession 先 API 后清本地（API 失败本地数据原封 + 失败提示）
 * - rebuildSessionFromThread 从 thread 重建会话条目（幂等/不覆盖真实记录）
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as sessionListStoreMod from '../sessionListStore'
import type * as useSessionsQueryMod from '@/hooks/queries/useSessionsQuery'
import type * as queryClientMod from '@/services/query/queryClient'
import type * as queryKeysMod from '@/services/query/queryKeys'
import type * as agentTabStoreMod from '@/stores/agentTabStore'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type * as sessionStoreMod from '@/stores/sessionStore'
import type { Session } from '@/types/models'
import type * as modelsMod from '@/types/models'

// ── Mock 所有外部依赖 ──
const mockGetSessions = vi.fn()
const mockCreateSessionApi = vi.fn()
const mockDeleteSessionApi = vi.fn()
const mockUpdateSessionAgentApi = vi.fn()
const mockUpdateSessionApi = vi.fn()

vi.mock('@/services/api/session', () => ({
  getSessions: mockGetSessions,
  createSession: mockCreateSessionApi,
  deleteSession: mockDeleteSessionApi,
  updateSessionAgent: mockUpdateSessionAgentApi,
  updateSession: mockUpdateSessionApi,
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  },
}))

let mockWatchlistStore: unknown[] = []
const mockSetLastActiveSession = vi.fn()
const mockGetLastActiveSession = vi.fn()
vi.mock('@/utils/storage', () => ({
  uiStorage: {
    setLastActiveSession: (...args: unknown[]) => mockSetLastActiveSession(...args),
    getLastActiveSession: () => mockGetLastActiveSession(),
    // 会话登记表（sessionWatchlist 消费）：内存假体，跟随真实接口面
    setSessionWatchlist: (list: unknown[]) => {
      mockWatchlistStore = list
    },
    getSessionWatchlist: () => mockWatchlistStore,
  },
  STORAGE_KEYS: { LAST_ACTIVE_SESSION: 'last_active_session' },
}))

vi.mock('@/stores/agentStore', () => ({
  useAgentStore: {
    getState: () => ({
      agents: [],
      setCurrentAgentId: vi.fn(),
    }),
    setState: vi.fn(),
  },
}))

const mockInitSessionTabs = vi.fn()
const mockUpdateTab = vi.fn()
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: {
    getState: () => ({
      initSessionTabs: mockInitSessionTabs,
      getTabIdByPipeline: vi.fn(),
      resetAllTabs: vi.fn(),
      updateTab: mockUpdateTab,
      currentSessionId: null,
    }),
    setState: vi.fn(),
  },
}))

vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: {
    getState: () => ({ bumpWorkspaceDataVersion: vi.fn() }),
    setState: vi.fn(),
  },
}))

const mockRegisterPipeline = vi.fn()
const mockActivatePipeline = vi.fn()
const mockStopStreaming = vi.fn()
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: {
    getState: () => ({
      registerPipeline: mockRegisterPipeline,
      activatePipeline: mockActivatePipeline,
      stopStreaming: mockStopStreaming,
      fetchMessages: vi.fn(),
      isStreaming: vi.fn(),
      getMessages: vi.fn(() => []),
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: {},
      streamingState: {},
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    }),
    setState: vi.fn(),
  },
}))

vi.mock('@/stores/sessionStore', () => {
  let _state: Record<string, unknown> = {
    activeSessionId: null,
    deletingSessionIds: new Set<string>(),
  }
  return {
    useSessionStore: {
      getState: () => _state,
      setState: (fn: (prev: Record<string, unknown>) => Record<string, unknown> | Partial<Record<string, unknown>>) => {
        const partial = typeof fn === 'function' ? fn(_state) : fn
        _state = { ..._state, ...partial }
      },
    },
  }
})

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    sendCancel: vi.fn(),
  },
}))

/** 创建测试用 Session 对象 */
function makeSession(overrides: Partial<modelsMod.Session> = {}) {
  return {
    id: 'sess-001',
    title: '灵汐',
    agentId: null,
    activePipelineId: 'pipe-001',
    pipelineIds: ['pipe-001'],
    starred: false,
    pinned: false,
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  } as modelsMod.Session
}

/** 写路径族共用：播种会话 + 桩定更新 API 成功（seed 为 describe 作用域函数） */
function seedSessionsWithUpdateApi(seed: (s: Session[]) => void, sessions: Session[]) {
  seed(sessions)
  mockUpdateSessionApi.mockResolvedValue({})
}

describe('sessionListStore', () => {
  let useSessionListStore: sessionListStoreMod.useSessionListStore
  let useSessionStore: sessionStoreMod.useSessionStore
  let queryClient: queryClientMod['queryClient']
  let queryKeys: queryKeysMod['queryKeys']
  let readSessions: useSessionsQueryMod['readSessions']

  /** 播种会话列表到 query cache（sessions 数据源已 query 化） */
  function seedSessions(sessions: Session[]): void {
    queryClient.setQueryData(queryKeys.sessions, sessions)
  }

  beforeEach(async () => {
    vi.resetModules()
    ;({ queryClient } = await import('@/services/query/queryClient'))
    ;({ queryKeys } = await import('@/services/query/queryKeys'))
    ;({ readSessions } = await import('@/hooks/queries/useSessionsQuery'))
    // 重置 sessionStore mock 的内部状态
    const sessionStoreModule = await import('@/stores/sessionStore')
    useSessionStore = sessionStoreModule.useSessionStore
    useSessionStore.setState(() => ({
      activeSessionId: null,
      deletingSessionIds: new Set<string>(),
    }))
    // sessions 数据已迁 query cache：每测试清缓存保证隔离
    queryClient.clear()
    // 重新导入 sessionListStore
    const mod = await import('../sessionListStore')
    useSessionListStore = mod.useSessionListStore
    // 重置 mock 调用记录
    mockUpdateSessionApi.mockReset()
    mockCreateSessionApi.mockReset()
    mockDeleteSessionApi.mockReset()
    mockInitSessionTabs.mockReset()
    mockSetLastActiveSession.mockReset()
    mockRegisterPipeline.mockReset()
    mockActivatePipeline.mockReset()
    mockStopStreaming.mockReset()
    mockWatchlistStore = []
    // vi.resetModules 不重置 mock 注册表：vi.mock 工厂内的 vi.fn()（setState/
    // sendCancel）跨测试共享同一实例，需手动清调用记录，防止上一用例的清理链
    // 调用泄入下一用例的「未发生」断言
    const pipelineStoreMock = await import('@/stores/pipelineMessageStore')
    vi.mocked(pipelineStoreMock.usePipelineMessageStore.setState).mockClear()
    const wsMock = await import('@/services/websocket/GlobalWebSocket')
    vi.mocked(wsMock.globalWS.sendCancel).mockClear()
  })

  // ── searchSessions ──

  describe('searchSessions', () => {
    it('空关键词返回全部会话（原样返回，不排序）', () => {
      const sessions = [
        makeSession({ id: 's1', title: '会话1', pinned: false }),
        makeSession({ id: 's2', title: '会话2', pinned: true }),
      ]
      seedSessions(sessions)

      const result = useSessionListStore.getState().searchSessions('')
      expect(result).toHaveLength(2)
      // 现行契约：空关键词早退原样返回（保持传入顺序，不做置顶/时间排序）
      expect(result.map((s) => s.id)).toEqual(['s1', 's2'])
    })

    it('按关键词过滤会话（不区分大小写）', () => {
      const sessions = [
        makeSession({ id: 's1', title: 'Python开发' }),
        makeSession({ id: 's2', title: 'React前端' }),
        makeSession({ id: 's3', title: 'python数据分析' }),
      ]
      seedSessions(sessions)

      const result = useSessionListStore.getState().searchSessions('python')
      expect(result).toHaveLength(2)
      expect(result.every((s) => s.title.toLowerCase().includes('python'))).toBe(true)
    })

    it('排序只看最近活跃：置顶不浮顶（R10 单点排序，侧栏与搜索同序）', () => {
      const sessions = [
        makeSession({ id: 's1', title: '普通会话', pinned: false, updatedAt: '2026-06-01T00:00:00.000Z' }),
        makeSession({ id: 's2', title: '置顶会话', pinned: true, updatedAt: '2026-05-01T00:00:00.000Z' }),
      ]
      seedSessions(sessions)

      // R10 契约：置顶/星标只作视觉标记不参与排序，更新的普通会话在前
      const result = useSessionListStore.getState().searchSessions('会话')
      expect(result[0].id).toBe('s1')
      expect(result[1].id).toBe('s2')
    })

    it('未置顶会话按更新时间倒序排列（非空关键词触发排序）', () => {
      const sessions = [
        makeSession({ id: 's1', title: '旧会话', pinned: false, updatedAt: '2026-01-01T00:00:00.000Z' }),
        makeSession({ id: 's2', title: '新会话', pinned: false, updatedAt: '2026-06-01T00:00:00.000Z' }),
      ]
      seedSessions(sessions)

      const result = useSessionListStore.getState().searchSessions('会话')
      expect(result[0].id).toBe('s2')
      expect(result[1].id).toBe('s1')
    })
  })

  // ── updateSession ──

  describe('updateSession', () => {
    it('更新指定会话的属性', () => {
      const sessions = [makeSession({ id: 's1', title: '旧标题' })]
      seedSessions(sessions)

      useSessionListStore.getState().updateSession('s1', { title: '新标题' })

      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.title).toBe('新标题')
    })

    it('更新时自动设置 updatedAt', () => {
      const sessions = [makeSession({ id: 's1', updatedAt: '2026-01-01T00:00:00.000Z' })]
      seedSessions(sessions)

      const before = new Date().getTime()
      useSessionListStore.getState().updateSession('s1', { title: '改了' })
      const after = new Date().getTime()

      const updated = readSessions().find((s) => s.id === 's1')
      const updatedTime = new Date(updated!.updatedAt!).getTime()
      expect(updatedTime).toBeGreaterThanOrEqual(before)
      expect(updatedTime).toBeLessThanOrEqual(after)
    })

    it('未匹配的会话不会被修改', () => {
      const sessions = [makeSession({ id: 's1', title: '原始' })]
      seedSessions(sessions)

      useSessionListStore.getState().updateSession('s-nonexistent', { title: '改动' })

      const unchanged = readSessions().find((s) => s.id === 's1')
      expect(unchanged?.title).toBe('原始')
    })
  })

  // ── updateSessionWorkspace ──

  describe('updateSessionWorkspace', () => {
    it('PATCH metadata 携带 workspace/workspace_mode，缓存同步更新（聚合真值源生效）', async () => {
      const sessions = [makeSession({ id: 's1' })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

      await useSessionListStore
        .getState()
        .updateSessionWorkspace('s1', 'D:/proj/demo', 'plain')

      expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', {
        metadata: { workspace: 'D:/proj/demo', workspace_mode: 'plain' },
      })
      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.workspace).toBe('D:/proj/demo')
      expect(updated?.workspaceMode).toBe('plain')
    })

    it('未传 workspaceMode 时只 PATCH workspace 键（不虚构拓扑值）', async () => {
      const sessions = [makeSession({ id: 's1' })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

      await useSessionListStore.getState().updateSessionWorkspace('s1', 'D:/ws/only')

      expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', {
        metadata: { workspace: 'D:/ws/only' },
      })
      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.workspace).toBe('D:/ws/only')
      expect(updated?.workspaceMode).toBeUndefined()
    })

    it('API 失败时拒绝且缓存不动（可重试）', async () => {
      const sessions = [makeSession({ id: 's1' })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockRejectedValue(new Error('网络失败'))

      await expect(
        useSessionListStore.getState().updateSessionWorkspace('s1', 'D:/proj/x'),
      ).rejects.toThrow('网络失败')

      const unchanged = readSessions().find((s) => s.id === 's1')
      expect(unchanged?.workspace).toBeUndefined()
      expect(unchanged?.pinned).toBe(false)
    })
  })

  // ── toggleSessionStar ──

  describe('toggleSessionStar', () => {
    it('从 false 切换为 true', () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      useSessionListStore.getState().toggleSessionStar('s1')

      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.starred).toBe(true)
    })

    it('从 true 切换为 false', () => {
      const sessions = [makeSession({ id: 's1', starred: true })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      useSessionListStore.getState().toggleSessionStar('s1')

      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.starred).toBe(false)
    })

    it('异步持久化到后端', async () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      useSessionListStore.getState().toggleSessionStar('s1')

      // 等待微任务完成
      await vi.waitFor(() => {
        expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', { metadata: { starred: true } })
      })
    })

    it('持久化失败不抛出异常', async () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockRejectedValue(new Error('网络错误'))

      // 不应抛出
      expect(() => useSessionListStore.getState().toggleSessionStar('s1')).not.toThrow()

      // 状态仍已本地更新
      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.starred).toBe(true)
    })
  })

  // ── toggleSessionPin ──

  describe('toggleSessionPin', () => {
    it('切换置顶状态', () => {
      const sessions = [makeSession({ id: 's1', pinned: false })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      useSessionListStore.getState().toggleSessionPin('s1')
      expect(readSessions().find((s) => s.id === 's1')?.pinned).toBe(true)

      useSessionListStore.getState().toggleSessionPin('s1')
      expect(readSessions().find((s) => s.id === 's1')?.pinned).toBe(false)
    })

    it('置顶/取消置顶均以 metadata.pinned 持久化到后端（BUG-78 契约锁：PATCH metadata 必须落库）', async () => {
      const sessions = [makeSession({ id: 's1', pinned: false })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      useSessionListStore.getState().toggleSessionPin('s1')
      await vi.waitFor(() => {
        expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', { metadata: { pinned: true } })
      })

      useSessionListStore.getState().toggleSessionPin('s1')
      await vi.waitFor(() => {
        expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', { metadata: { pinned: false } })
      })
    })
  })

  // ── renameSession ──

  describe('renameSession', () => {
    it('更新本地标题并调用 API', async () => {
      const sessions = [makeSession({ id: 's1', title: '旧名' })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      await useSessionListStore.getState().renameSession('s1', '  新名字  ')

      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.title).toBe('新名字') // trim 处理
      expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', { title: '新名字' })
    })

    it('空标题不执行任何操作', async () => {
      const sessions = [makeSession({ id: 's1', title: '原始名' })]
      seedSessions(sessions)

      await useSessionListStore.getState().renameSession('s1', '   ')

      const unchanged = readSessions().find((s) => s.id === 's1')
      expect(unchanged?.title).toBe('原始名')
      expect(mockUpdateSessionApi).not.toHaveBeenCalled()
    })

    it('API 失败时回滚本地标题（乐观更新失败回滚）', async () => {
      const sessions = [makeSession({ id: 's1', title: '旧名' })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockRejectedValue(new Error('网络错误'))

      await useSessionListStore.getState().renameSession('s1', '新名')

      // 现行契约：乐观更新后若 API 失败，回滚到原标题（避免与服务端状态漂移）
      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.title).toBe('旧名')
    })
  })

  // ── setActiveSession 防护 ──

  describe('setActiveSession 边界', () => {
    it('空字符串 ID 不执行操作', async () => {
      const sessions = [makeSession({ id: 's1' })]
      seedSessions(sessions)
      useSessionStore.setState(() => ({ activeSessionId: null }))

      await useSessionListStore.getState().setActiveSession('')
      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })

    it('不存在的会话 ID 不执行操作', async () => {
      const sessions = [makeSession({ id: 's1' })]
      seedSessions(sessions)
      useSessionStore.setState(() => ({ activeSessionId: null }))

      await useSessionListStore.getState().setActiveSession('nonexistent')
      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })

    it('有效 ID 设置为活跃会话', async () => {
      const sessions = [makeSession({ id: 's1' })]
      seedSessions(sessions)
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockSetLastActiveSession.mockReset()

      await useSessionListStore.getState().setActiveSession('s1', false)

      expect(useSessionStore.getState().activeSessionId).toBe('s1')
      expect(mockSetLastActiveSession).toHaveBeenCalledWith('s1')
    })
  })

  // ── autoRenameSessionIfNeeded ──

  describe('autoRenameSessionIfNeeded', () => {
    it('默认标题时根据首条用户消息重命名', async () => {
      const sessions = [makeSession({ id: 's1', title: '灵汐' })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      // Mock getMessages 返回一条用户消息
      const { usePipelineMessageStore } = await import('@/stores/pipelineMessageStore')
      const origGetState = usePipelineMessageStore.getState
      usePipelineMessageStore.getState = () => ({
        ...origGetState(),
        getMessages: () => [{ role: 'user', content: '帮我写一个排序算法', parts: [] }],
      })

      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      await vi.waitFor(() => {
        const updated = readSessions().find((s) => s.id === 's1')
        expect(updated?.title).toBe('帮我写一个排序算法')
      })

      usePipelineMessageStore.getState = origGetState
    })

    it('超过30字符截断加省略号', async () => {
      const longText = '这是一段非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的消息'
      const sessions = [makeSession({ id: 's1', title: '灵汐' })]
      seedSessionsWithUpdateApi(seedSessions, sessions)

      const { usePipelineMessageStore } = await import('@/stores/pipelineMessageStore')
      const origGetState = usePipelineMessageStore.getState
      usePipelineMessageStore.getState = () => ({
        ...origGetState(),
        getMessages: () => [{ role: 'user', content: longText, parts: [] }],
      })

      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      await vi.waitFor(() => {
        const updated = readSessions().find((s) => s.id === 's1')
        expect(updated?.title.length).toBeLessThanOrEqual(31) // 30 + …
        expect(updated?.title).toMatch(/…$/)
      })

      usePipelineMessageStore.getState = origGetState
    })

    it('非默认标题时不重命名', () => {
      const sessions = [makeSession({ id: 's1', title: '用户自定义标题' })]
      seedSessions(sessions)

      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      const unchanged = readSessions().find((s) => s.id === 's1')
      expect(unchanged?.title).toBe('用户自定义标题')
      expect(mockUpdateSessionApi).not.toHaveBeenCalled()
    })

    it('会话不存在时不操作', () => {

      expect(() =>
        useSessionListStore.getState().autoRenameSessionIfNeeded('nonexistent', 'pipe-001'),
      ).not.toThrow()
    })

    it('无用户消息时不操作', () => {
      const sessions = [makeSession({ id: 's1', title: '灵汐' })]
      seedSessions(sessions)

      // getMessages 返回空数组或只有 assistant 消息
      // 默认 mock 的 getMessages 返回 []，所以不会触发重命名
      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      expect(mockUpdateSessionApi).not.toHaveBeenCalled()
    })
  })

  // ── createSession：会话面同步初始化（标签/输入框随 activeSessionId 切换） ──

  describe('createSession', () => {
    it('创建后切换选中并重建标签面（顶部标签/输入框状态随 activeTabId 更新）', async () => {
      const newSession = makeSession({ id: 'sess-new', title: '新会话', agentId: 'agent-1' })
      mockCreateSessionApi.mockResolvedValue(newSession)
      useSessionStore.setState(() => ({ activeSessionId: null }))

      const created = await useSessionListStore.getState().createSession()

      expect(created.id).toBe('sess-new')
      // 选中会话切到新会话（侧边栏高亮/聊天区渲染依据）
      expect(useSessionStore.getState().activeSessionId).toBe('sess-new')
      // 标签面以新会话重建——缺失会致顶部标签/输入框草稿停留上一会话
      expect(mockInitSessionTabs).toHaveBeenCalledWith('sess-new')
      // 选中会话持久化（刷新恢复链）
      expect(mockSetLastActiveSession).toHaveBeenCalledWith('sess-new')
      // 新管道注册并激活（消息区切到新管道）
      expect(mockRegisterPipeline).toHaveBeenCalledWith(
        expect.objectContaining({ pipelineId: 'pipe-001', sessionId: 'sess-new' }),
      )
      expect(mockActivatePipeline).toHaveBeenCalledWith('pipe-001')
      // 新会话已进入列表缓存
      expect(readSessions().some((s) => s.id === 'sess-new')).toBe(true)
    })

    it('新会话无管道时同样重建标签面（不残留上一会话管道激活）', async () => {
      const newSession = makeSession({
        id: 'sess-none',
        activePipelineId: null,
        pipelineIds: [],
      })
      mockCreateSessionApi.mockResolvedValue(newSession)

      await useSessionListStore.getState().createSession()

      expect(mockInitSessionTabs).toHaveBeenCalledWith('sess-none')
      expect(mockSetLastActiveSession).toHaveBeenCalledWith('sess-none')
      // 无管道不注册/激活（标签面重建由 initSessionTabs 决定管道激活）
      expect(mockRegisterPipeline).not.toHaveBeenCalled()
      expect(mockActivatePipeline).not.toHaveBeenCalled()
    })
  })

  // ── copySession ──

  describe('copySession', () => {
    it('创建副本会话', async () => {
      const original = makeSession({ id: 's1', title: '原始会话', agentId: 'agent-1' })
      seedSessions([original])

      const newSession = makeSession({ id: 's2', title: '原始会话 (副本)' })
      mockCreateSessionApi.mockResolvedValue(newSession)

      const result = await useSessionListStore.getState().copySession('s1')

      expect(mockCreateSessionApi).toHaveBeenCalledWith({
        title: '原始会话 (副本)',
        agentId: 'agent-1',
      })
      expect(result.title).toBe('原始会话 (副本)')
    })

    it('不存在的会话抛出错误', async () => {

      await expect(
        useSessionListStore.getState().copySession('nonexistent'),
      ).rejects.toThrow('会话不存在')
    })
  })

  // ── updateSessionAgent：切换 Agent 后同步主 Tab ──

  describe('updateSessionAgent', () => {
    it('当前活跃会话切换 Agent 后，同步刷新主 Tab 的 agentId', async () => {
      const sessions = [makeSession({ id: 's1', agentId: 'old-agent' })]
      seedSessions(sessions)
      mockUpdateSessionAgentApi.mockResolvedValue({
        agentId: 'new-agent',
        updatedAt: '2026-07-02T00:00:00.000Z',
      })

      // 临时覆写 agentTabStore.getState，模拟当前会话已初始化主 Tab
      const { useAgentTabStore } = await import('@/stores/agentTabStore')
      const origGetState = useAgentTabStore.getState
      const updateTab = vi.fn()
      const saveCurrentTabs = vi.fn()
      useAgentTabStore.getState = () => ({
        currentSessionId: 's1',
        tabs: [{ id: 'main-s1', agentLevel: 1, agentId: 'old-agent' }],
        updateTab,
        saveCurrentTabs,
      }) as any

      await useSessionListStore.getState().updateSessionAgent('s1', 'new-agent')

      // 主 Tab 的 agentId 被同步为新值
      expect(updateTab).toHaveBeenCalledWith('main-s1', { agentId: 'new-agent' })
      expect(saveCurrentTabs).toHaveBeenCalled()

      useAgentTabStore.getState = origGetState
    })

    it('非当前活跃会话切换 Agent 时，不触碰 agentTabStore', async () => {
      const sessions = [makeSession({ id: 's1', agentId: 'old-agent' })]
      seedSessions(sessions)
      mockUpdateSessionAgentApi.mockResolvedValue({
        agentId: 'new-agent',
        updatedAt: '2026-07-02T00:00:00.000Z',
      })

      const { useAgentTabStore } = await import('@/stores/agentTabStore')
      const origGetState = useAgentTabStore.getState
      const updateTab = vi.fn()
      const saveCurrentTabs = vi.fn()
      // currentSessionId 指向另一个会话
      useAgentTabStore.getState = () => ({
        currentSessionId: 's-other',
        tabs: [{ id: 'main-s-other', agentLevel: 1, agentId: 'x' }],
        updateTab,
        saveCurrentTabs,
      }) as any

      await useSessionListStore.getState().updateSessionAgent('s1', 'new-agent')

      expect(updateTab).not.toHaveBeenCalled()
      expect(saveCurrentTabs).not.toHaveBeenCalled()

      useAgentTabStore.getState = origGetState
    })
  })

  // ── deleteSession：先 API 后清本地（失败可恢复）──

  describe('deleteSession', () => {
    it('API 成功 → 删除成立后执行完整清理链（取消信号/清管道数据/清缓存/清选中态）', async () => {
      const sessions = [makeSession({ id: 's-del' })]
      seedSessions(sessions)
      useSessionStore.setState(() => ({ activeSessionId: 's-del', deletingSessionIds: new Set<string>() }))
      mockDeleteSessionApi.mockResolvedValue(undefined)

      const { usePipelineMessageStore } = await import('@/stores/pipelineMessageStore')
      const { globalWS } = await import('@/services/websocket/GlobalWebSocket')
      // 播种该会话名下管道（取消信号/停流按 pipelineSessionMap 派生管道逐个发）
      const origGetState = usePipelineMessageStore.getState
      usePipelineMessageStore.getState = () => ({
        ...origGetState(),
        pipelineSessionMap: { 'pipe-del': 's-del' },
      })

      await useSessionListStore.getState().deleteSession('s-del')

      expect(mockDeleteSessionApi).toHaveBeenCalledWith('s-del')
      // 删除成立后才发取消信号（随消息取消在途流）+ 停流
      expect(globalWS.sendCancel).toHaveBeenCalledWith('s-del', '会话已删除', 'pipe-del')
      expect(mockStopStreaming).toHaveBeenCalledWith('pipe-del')
      expect(mockStopStreaming).toHaveBeenCalledWith('s-del') // 会话 ID 兜底主管道
      expect(usePipelineMessageStore.setState).toHaveBeenCalled()

      usePipelineMessageStore.getState = origGetState
      // 会话已从列表缓存移除
      expect(readSessions().some((s) => s.id === 's-del')).toBe(false)
      // 删除的就是活跃会话 → 选中态清空、deleting 标记移除
      expect(useSessionStore.getState().activeSessionId).toBeNull()
      expect(useSessionStore.getState().deletingSessionIds.has('s-del')).toBe(false)
    })

    it('API 失败 → 本地数据原封不动（列表/选中态保留，清理链未执行）+ 用户可见失败提示', async () => {
      const sessions = [
        makeSession({ id: 's-keep', title: '要保留的会话' }),
        makeSession({ id: 's-other' }),
      ]
      seedSessions(sessions)
      useSessionStore.setState(() => ({ activeSessionId: 's-keep', deletingSessionIds: new Set<string>() }))
      mockDeleteSessionApi.mockRejectedValue(new Error('后端 500'))

      const { usePipelineMessageStore } = await import('@/stores/pipelineMessageStore')
      const { globalWS } = await import('@/services/websocket/GlobalWebSocket')
      const { useNotificationStore } = await import('@/stores/notificationStore')

      await expect(
        useSessionListStore.getState().deleteSession('s-keep'),
      ).rejects.toThrow('后端 500')

      // 本地数据原封：目标会话与其他会话都还在列表缓存
      expect(readSessions().some((s) => s.id === 's-keep')).toBe(true)
      expect(readSessions()).toHaveLength(2)
      // 清理链未执行：无取消信号、无管道数据清理
      expect(globalWS.sendCancel).not.toHaveBeenCalled()
      expect(usePipelineMessageStore.setState).not.toHaveBeenCalled()
      // 选中态未被动
      expect(useSessionStore.getState().activeSessionId).toBe('s-keep')
      // deleting 标记已移除（用户可直接重试）
      expect(useSessionStore.getState().deletingSessionIds.has('s-keep')).toBe(false)
      // 用户可见失败提示
      expect(
        useNotificationStore.getState().notifications.some((n) => n.title === '删除失败，数据未变'),
      ).toBe(true)
    })
  })

  // ── restoreActiveSessionIfNeeded（R10② 冷进入直载最近会话） ──

  describe('restoreActiveSessionIfNeeded', () => {
    it('无选中 + last_active 命中列表 → 恢复为活跃会话并持久化', async () => {
      seedSessions([makeSession({ id: 's1' }), makeSession({ id: 's2' })])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue('s2')
      mockSetLastActiveSession.mockReset()

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      expect(useSessionStore.getState().activeSessionId).toBe('s2')
      expect(mockSetLastActiveSession).toHaveBeenCalledWith('s2')
    })

    it('已有有效选中（后台刷新重跑）→ 幂等不动作', async () => {
      seedSessions([makeSession({ id: 's1' }), makeSession({ id: 's2' })])
      useSessionStore.setState(() => ({ activeSessionId: 's1' }))
      mockGetLastActiveSession.mockReturnValue('s2')
      mockSetLastActiveSession.mockReset()

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      // 选中保持 s1，未被 last_active 覆盖
      expect(useSessionStore.getState().activeSessionId).toBe('s1')
      expect(mockSetLastActiveSession).not.toHaveBeenCalled()
    })

    it('无持久化选中且有会话 → 直载最近会话（排序后第一个），不停在欢迎页', async () => {
      // 列表乱序：s-old 更旧但排在数组前（排序真值不看传入顺序）
      seedSessions([
        makeSession({ id: 's-old', updatedAt: '2026-01-01T00:00:00.000Z' }),
        makeSession({ id: 's-new', updatedAt: '2026-06-01T00:00:00.000Z' }),
      ])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue(null)

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      expect(useSessionStore.getState().activeSessionId).toBe('s-new')
    })

    it('last_active 不在列表（会话已删）→ 回落直载最近会话', async () => {
      seedSessions([makeSession({ id: 's1', updatedAt: '2026-03-01T00:00:00.000Z' })])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue('gone-session')

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      expect(useSessionStore.getState().activeSessionId).toBe('s1')
    })

    it('无会话 → 保持无选中（欢迎页），不报错', async () => {
      seedSessions([])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue(null)

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })

    it('直载只发生一次：机会消耗后回到无选中态（如删会话）不再自动抓会话', async () => {
      seedSessions([makeSession({ id: 's1' }), makeSession({ id: 's2' })])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue(null)

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())
      expect(useSessionStore.getState().activeSessionId).toBe('s1')

      // 用户回到无选中态（删除当前会话链路），后台刷新带重跑本函数
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue(null)
      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      // 不被劫持：保持无选中（欢迎页）
      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })

    it('加载失败：错误向上传播且选中保持空（主界面回落欢迎页，不吞异常）', async () => {
      seedSessions([makeSession({ id: 's1' })])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue(null)
      const originalSetActive = useSessionListStore.getState().setActiveSession
      useSessionListStore.setState({
        setActiveSession: vi.fn().mockRejectedValue(new Error('加载失败')),
      } as never)

      try {
        await expect(
          useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions()),
        ).rejects.toThrow('加载失败')
        expect(useSessionStore.getState().activeSessionId).toBeNull()
      } finally {
        useSessionListStore.setState({ setActiveSession: originalSetActive } as never)
      }
    })
  })

  // ── rebuildSessionFromThread（会话记录缺失补救面） ──

  describe('rebuildSessionFromThread', () => {
    it('缓存缺失 → 合成最小会话条目（标题/主管道绑定），可被 setActiveSession 消费', () => {
      useSessionListStore.getState().rebuildSessionFromThread('thread-abc', {
        title: '续棒2-推理框架调研',
        pipelineId: 'pipe-task',
      })

      const rebuilt = readSessions().find((s) => s.id === 'thread-abc')
      expect(rebuilt).toBeDefined()
      expect(rebuilt!.title).toBe('续棒2-推理框架调研')
      // 主管道真值 = pipelineIds[0]（mainPipelineIdOf 契约）：initSessionTabs
      // 据此把主标签绑到任务管道，消息加载即任务对话
      expect(rebuilt!.pipelineIds).toEqual(['pipe-task'])
      expect(rebuilt!.createdAt).toBeTruthy()
      expect(rebuilt!.updatedAt).toBeTruthy()
    })

    it('hints 缺省 → 标题回退「未命名会话」、pipelineIds 空数组', () => {
      useSessionListStore.getState().rebuildSessionFromThread('thread-xyz')

      const rebuilt = readSessions().find((s) => s.id === 'thread-xyz')
      expect(rebuilt!.title).toBe('未命名会话')
      expect(rebuilt!.pipelineIds).toEqual([])
    })

    it('同 id 已存在（真实记录）→ 跳过不覆盖', () => {
      seedSessions([makeSession({ id: 'thread-dup', title: '真实会话', pipelineIds: ['pipe-real'] })])

      useSessionListStore.getState().rebuildSessionFromThread('thread-dup', {
        title: '合成标题',
        pipelineId: 'pipe-fake',
      })

      const kept = readSessions().find((s) => s.id === 'thread-dup')
      expect(kept!.title).toBe('真实会话')
      expect(kept!.pipelineIds).toEqual(['pipe-real'])
      expect(readSessions()).toHaveLength(1)
    })

    it('重复重建幂等（只写一条）', () => {
      useSessionListStore.getState().rebuildSessionFromThread('thread-idem', { title: 'A' })
      useSessionListStore.getState().rebuildSessionFromThread('thread-idem', { title: 'B' })

      const hits = readSessions().filter((s) => s.id === 'thread-idem')
      expect(hits).toHaveLength(1)
      expect(hits[0].title).toBe('A')
    })
  })
})
