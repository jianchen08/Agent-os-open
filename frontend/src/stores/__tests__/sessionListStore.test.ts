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

vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: {
    getState: () => ({
      initSessionTabs: vi.fn(),
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

vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: {
    getState: () => ({
      registerPipeline: vi.fn(),
      activatePipeline: vi.fn(),
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
    sessions: [],
    activeSessionId: null,
    isLoading: false,
    deletingSessionIds: new Set<string>(),
    error: null,
    messagePagination: {},
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

  beforeEach(async () => {
    vi.resetModules()
    // 重置 sessionStore mock 的内部状态
    const sessionStoreModule = await import('@/stores/sessionStore')
    useSessionStore = sessionStoreModule.useSessionStore
    useSessionStore.setState(() => ({
      sessions: [],
      activeSessionId: null,
      isLoading: false,
      deletingSessionIds: new Set<string>(),
      error: null,
      messagePagination: {},
    }))
    // 重新导入 sessionListStore
    const mod = await import('../sessionListStore')
    useSessionListStore = mod.useSessionListStore
    // 重置 mock 调用记录
    mockUpdateSessionApi.mockReset()
    mockCreateSessionApi.mockReset()
    mockDeleteSessionApi.mockReset()
  })

  // ── searchSessions ──

  describe('searchSessions', () => {
    it('空关键词返回全部会话', () => {
      const sessions = [makeSession({ id: 's1', title: '会话1' }), makeSession({ id: 's2', title: '会话2' })]
      useSessionStore.setState(() => ({ sessions }))

      const result = useSessionListStore.getState().searchSessions('')
      expect(result).toHaveLength(2)
    })

    it('按关键词过滤会话（不区分大小写）', () => {
      const sessions = [
        makeSession({ id: 's1', title: 'Python开发' }),
        makeSession({ id: 's2', title: 'React前端' }),
        makeSession({ id: 's3', title: 'python数据分析' }),
      ]
      useSessionStore.setState(() => ({ sessions }))

      const result = useSessionListStore.getState().searchSessions('python')
      expect(result).toHaveLength(2)
      expect(result.every((s) => s.title.toLowerCase().includes('python'))).toBe(true)
    })

    it('置顶会话排在前面', () => {
      const sessions = [
        makeSession({ id: 's1', title: '普通会话', pinned: false, updatedAt: '2026-06-01T00:00:00.000Z' }),
        makeSession({ id: 's2', title: '置顶会话', pinned: true, updatedAt: '2026-05-01T00:00:00.000Z' }),
      ]
      useSessionStore.setState(() => ({ sessions }))

      const result = useSessionListStore.getState().searchSessions('')
      expect(result[0].id).toBe('s2')
      expect(result[1].id).toBe('s1')
    })

    it('未置顶会话按更新时间倒序排列', () => {
      const sessions = [
        makeSession({ id: 's1', title: '旧会话', pinned: false, updatedAt: '2026-01-01T00:00:00.000Z' }),
        makeSession({ id: 's2', title: '新会话', pinned: false, updatedAt: '2026-06-01T00:00:00.000Z' }),
      ]
      useSessionStore.setState(() => ({ sessions }))

      const result = useSessionListStore.getState().searchSessions('')
      expect(result[0].id).toBe('s2')
      expect(result[1].id).toBe('s1')
    })
  })

  // ── updateSession ──

  describe('updateSession', () => {
    it('更新指定会话的属性', () => {
      const sessions = [makeSession({ id: 's1', title: '旧标题' })]
      useSessionStore.setState(() => ({ sessions }))

      useSessionListStore.getState().updateSession('s1', { title: '新标题' })

      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(updated?.title).toBe('新标题')
    })

    it('更新时自动设置 updatedAt', () => {
      const sessions = [makeSession({ id: 's1', updatedAt: '2026-01-01T00:00:00.000Z' })]
      useSessionStore.setState(() => ({ sessions }))

      const before = new Date().getTime()
      useSessionListStore.getState().updateSession('s1', { title: '改了' })
      const after = new Date().getTime()

      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      const updatedTime = new Date(updated!.updatedAt!).getTime()
      expect(updatedTime).toBeGreaterThanOrEqual(before)
      expect(updatedTime).toBeLessThanOrEqual(after)
    })

    it('未匹配的会话不会被修改', () => {
      const sessions = [makeSession({ id: 's1', title: '原始' })]
      useSessionStore.setState(() => ({ sessions }))

      useSessionListStore.getState().updateSession('s-nonexistent', { title: '改动' })

      const unchanged = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(unchanged?.title).toBe('原始')
    })
  })

  // ── toggleSessionStar ──

  describe('toggleSessionStar', () => {
    it('从 false 切换为 true', () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionStar('s1')

      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(updated?.starred).toBe(true)
    })

    it('从 true 切换为 false', () => {
      const sessions = [makeSession({ id: 's1', starred: true })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionStar('s1')

      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(updated?.starred).toBe(false)
    })

    it('异步持久化到后端', async () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionStar('s1')

      // 等待微任务完成
      await vi.waitFor(() => {
        expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', { metadata: { starred: true } })
      })
    })

    it('持久化失败不抛出异常', async () => {
      const sessions = [makeSession({ id: 's1', starred: false })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockRejectedValue(new Error('网络错误'))

      // 不应抛出
      expect(() => useSessionListStore.getState().toggleSessionStar('s1')).not.toThrow()

      // 状态仍已本地更新
      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(updated?.starred).toBe(true)
    })
  })

  // ── toggleSessionPin ──

  describe('toggleSessionPin', () => {
    it('切换置顶状态', () => {
      const sessions = [makeSession({ id: 's1', pinned: false })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockResolvedValue({})

      useSessionListStore.getState().toggleSessionPin('s1')
      expect(useSessionStore.getState().sessions.find((s) => s.id === 's1')?.pinned).toBe(true)

      useSessionListStore.getState().toggleSessionPin('s1')
      expect(useSessionStore.getState().sessions.find((s) => s.id === 's1')?.pinned).toBe(false)
    })
  })

  // ── renameSession ──

  describe('renameSession', () => {
    it('更新本地标题并调用 API', async () => {
      const sessions = [makeSession({ id: 's1', title: '旧名' })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockResolvedValue({})

      await useSessionListStore.getState().renameSession('s1', '  新名字  ')

      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(updated?.title).toBe('新名字') // trim 处理
      expect(mockUpdateSessionApi).toHaveBeenCalledWith('s1', { title: '新名字' })
    })

    it('空标题不执行任何操作', async () => {
      const sessions = [makeSession({ id: 's1', title: '原始名' })]
      useSessionStore.setState(() => ({ sessions }))

      await useSessionListStore.getState().renameSession('s1', '   ')

      const unchanged = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(unchanged?.title).toBe('原始名')
      expect(mockUpdateSessionApi).not.toHaveBeenCalled()
    })

    it('API 失败时本地标题已更新（乐观更新）', async () => {
      const sessions = [makeSession({ id: 's1', title: '旧名' })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockRejectedValue(new Error('网络错误'))

      await useSessionListStore.getState().renameSession('s1', '新名')

      const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(updated?.title).toBe('新名') // 乐观更新保留
    })
  })

  // ── setActiveSession 防护 ──

  describe('setActiveSession 边界', () => {
    it('空字符串 ID 不执行操作', async () => {
      const sessions = [makeSession({ id: 's1' })]
      useSessionStore.setState(() => ({ sessions, activeSessionId: null }))

      await useSessionListStore.getState().setActiveSession('')
      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })

    it('不存在的会话 ID 不执行操作', async () => {
      const sessions = [makeSession({ id: 's1' })]
      useSessionStore.setState(() => ({ sessions, activeSessionId: null }))

      await useSessionListStore.getState().setActiveSession('nonexistent')
      expect(useSessionStore.getState().activeSessionId).toBeNull()
    })

    it('有效 ID 设置为活跃会话', async () => {
      const sessions = [makeSession({ id: 's1' })]
      useSessionStore.setState(() => ({ sessions, activeSessionId: null }))
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
      useSessionStore.setState(() => ({ sessions }))
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
        const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
        expect(updated?.title).toBe('帮我写一个排序算法')
      })

      usePipelineMessageStore.getState = origGetState
    })

    it('超过30字符截断加省略号', async () => {
      const longText = '这是一段非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的消息'
      const sessions = [makeSession({ id: 's1', title: '灵汐' })]
      useSessionStore.setState(() => ({ sessions }))
      mockUpdateSessionApi.mockResolvedValue({})

      const { usePipelineMessageStore } = await import('@/stores/pipelineMessageStore')
      const origGetState = usePipelineMessageStore.getState
      usePipelineMessageStore.getState = () => ({
        ...origGetState(),
        getMessages: () => [{ role: 'user', content: longText, parts: [] }],
      })

      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      await vi.waitFor(() => {
        const updated = useSessionStore.getState().sessions.find((s) => s.id === 's1')
        expect(updated?.title.length).toBeLessThanOrEqual(31) // 30 + …
        expect(updated?.title).toMatch(/…$/)
      })

      usePipelineMessageStore.getState = origGetState
    })

    it('非默认标题时不重命名', () => {
      const sessions = [makeSession({ id: 's1', title: '用户自定义标题' })]
      useSessionStore.setState(() => ({ sessions }))

      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      const unchanged = useSessionStore.getState().sessions.find((s) => s.id === 's1')
      expect(unchanged?.title).toBe('用户自定义标题')
      expect(mockUpdateSessionApi).not.toHaveBeenCalled()
    })

    it('会话不存在时不操作', () => {
      useSessionStore.setState(() => ({ sessions: [] }))

      expect(() =>
        useSessionListStore.getState().autoRenameSessionIfNeeded('nonexistent', 'pipe-001'),
      ).not.toThrow()
    })

    it('无用户消息时不操作', () => {
      const sessions = [makeSession({ id: 's1', title: '灵汐' })]
      useSessionStore.setState(() => ({ sessions }))

      // getMessages 返回空数组或只有 assistant 消息
      // 默认 mock 的 getMessages 返回 []，所以不会触发重命名
      useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'pipe-001')

      expect(mockUpdateSessionApi).not.toHaveBeenCalled()
    })
  })

  // ── copySession ──

  describe('copySession', () => {
    it('创建副本会话', async () => {
      const original = makeSession({ id: 's1', title: '原始会话', agentId: 'agent-1' })
      useSessionStore.setState(() => ({ sessions: [original] }))

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
      useSessionStore.setState({ sessions: [] })

      await expect(
        useSessionListStore.getState().copySession('nonexistent'),
      ).rejects.toThrow('会话不存在')
    })
  })

  // ── updateSessionAgent：切换 Agent 后同步主 Tab ──

  describe('updateSessionAgent', () => {
    it('当前活跃会话切换 Agent 后，同步刷新主 Tab 的 agentId', async () => {
      const sessions = [makeSession({ id: 's1', agentId: 'old-agent' })]
      useSessionStore.setState({ sessions })
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
      useSessionStore.setState({ sessions })
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
})
