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
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Session } from '@/types/models'

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

const mockSetLastActiveSession = vi.fn()
const mockGetLastActiveSession = vi.fn()
vi.mock('@/utils/storage', () => ({
  uiStorage: {
    setLastActiveSession: (...args: unknown[]) => mockSetLastActiveSession(...args),
    getLastActiveSession: () => mockGetLastActiveSession(),
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
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: {
    getState: () => ({
      initSessionTabs: mockInitSessionTabs,
      getTabIdByPipeline: vi.fn(),
      resetAllTabs: vi.fn(),
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
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: {
    getState: () => ({
      registerPipeline: mockRegisterPipeline,
      activatePipeline: mockActivatePipeline,
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
function makeSession(overrides: Partial<import('@/types/models').Session> = {}) {
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
  } as import('@/types/models').Session
}

describe('sessionListStore', () => {
  let useSessionListStore: typeof import('../sessionListStore').useSessionListStore
  let useSessionStore: typeof import('@/stores/sessionStore').useSessionStore
  let queryClient: typeof import('@/services/query/queryClient')['queryClient']
  let queryKeys: typeof import('@/services/query/queryKeys')['queryKeys']
  let readSessions: typeof import('@/hooks/queries/useSessionsQuery')['readSessions']

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

    it('置顶会话排在前面（非空关键词触发排序）', () => {
      const sessions = [
        makeSession({ id: 's1', title: '普通会话', pinned: false, updatedAt: '2026-06-01T00:00:00.000Z' }),
        makeSession({ id: 's2', title: '置顶会话', pinned: true, updatedAt: '2026-05-01T00:00:00.000Z' }),
      ]
      seedSessions(sessions)

      // 现行契约：空关键词早退不排序，排序仅在非空关键词过滤后进行
      const result = useSessionListStore.getState().searchSessions('会话')
      expect(result[0].id).toBe('s2')
      expect(result[1].id).toBe('s1')
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

  // ── toggleSessionStar ──

  describe('toggleSessionStar', () => {
    it('从 false 切换为 true', () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionStar('s1')

      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.starred).toBe(true)
    })

    it('从 true 切换为 false', () => {
      const sessions = [makeSession({ id: 's1', starred: true })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionStar('s1')

      const updated = readSessions().find((s) => s.id === 's1')
      expect(updated?.starred).toBe(false)
    })

    it('异步持久化到后端', async () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

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
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionPin('s1')
      expect(readSessions().find((s) => s.id === 's1')?.pinned).toBe(true)

      useSessionListStore.getState().toggleSessionPin('s1')
      expect(readSessions().find((s) => s.id === 's1')?.pinned).toBe(false)
    })
  })

  // ── renameSession ──

  describe('renameSession', () => {
    it('更新本地标题并调用 API', async () => {
      const sessions = [makeSession({ id: 's1', title: '旧名' })]
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

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
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

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
      seedSessions(sessions)
      mockUpdateSessionApi.mockResolvedValue({})

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

  // ── restoreActiveSessionIfNeeded（query 化后的刷新恢复链） ──

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

    it('last_active 不在列表（会话已删）→ 保持无选中', async () => {
      seedSessions([makeSession({ id: 's1' })])
      useSessionStore.setState(() => ({ activeSessionId: null }))
      mockGetLastActiveSession.mockReturnValue('gone-session')

      await useSessionListStore.getState().restoreActiveSessionIfNeeded(readSessions())

      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })
  })
})
