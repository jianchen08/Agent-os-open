/**
 * AgentTabStore 配额降级清理测试
 *
 * Bug 场景：localStorage 配额不足时，saveTabsToStorage 的降级清理
 * 原本只清 agent-tabs-* 前缀（几 KB），碰不到真正占空间的 pipeline-messages key，
 * 导致清理无效、重试仍失败、warn 反复刷。
 *
 * 修复后：降级清理同时删除残留的 pipeline-messages localStorage key，释放空间。
 *
 * 本测试通过 mock localStorage.setItem 抛 QuotaExceededError，
 * 验证触发 saveCurrentTabs 后 pipeline-messages 被清理。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

// sessionStore / pipelineMessageStore 依赖需 mock，避免拉起完整网络链路
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: {
    getState: () => ({
      activatePipeline: vi.fn(),
      registerPipeline: vi.fn(),
      getMessages: vi.fn(() => []),
      initFromAPI: vi.fn(),
      loadPipelineMessages: vi.fn(() => Promise.resolve({ ok: true })),
      pipelines: {},
      messagesByPipeline: {},
    }),
  },
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: {
    getState: () => ({
      sessions: [{ id: 'sess-1', pipelineIds: ['pid-main'], agentId: 'lingxi' }],
    }),
  },
}))

describe('AgentTabStore 配额降级清理', () => {
  let useAgentTabStore: typeof import('@/stores/agentTabStore').useAgentTabStore
  let originalSetItem: typeof Storage.prototype.setItem

  beforeEach(async () => {
    localStorage.clear()
    originalSetItem = Storage.prototype.setItem

    vi.resetModules()
    const mod = await import('@/stores/agentTabStore')
    useAgentTabStore = mod.useAgentTabStore
    // 初始化会话，建立 currentSessionId
    useAgentTabStore.getState().initSessionTabs('sess-1')
  })

  afterEach(() => {
    Storage.prototype.setItem = originalSetItem
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('配额不足时，降级清理应删除残留的 pipeline-messages localStorage key', () => {
    // 预置：localStorage 里残留旧的 pipeline-messages（模拟迁移前的遗留数据）
    localStorage.setItem('pipeline-messages', '{"old":"data"}')
    expect(localStorage.getItem('pipeline-messages')).not.toBeNull()

    // mock：所有 setItem 都抛 QuotaExceededError，模拟配额已满
    Storage.prototype.setItem = vi.fn(() => {
      throw new DOMException('quota exceeded', 'QuotaExceededError')
    })

    // 触发 saveCurrentTabs（addTab 内部会调用）
    // 不应抛异常（降级清理吞掉错误）
    expect(() => {
      useAgentTabStore.getState().addTab({
        id: 'sub-1',
        agentId: 'agent-x',
        agentName: '子Agent',
        agentLevel: 2,
        path: ['主Agent', '子Agent'],
        status: 'running',
        hasUnread: false,
        canClose: true,
      })
    }).not.toThrow()

    // 降级清理应已尝试删除 pipeline-messages：
    // 注意 cleanupExpiredSessionData 内部调用 localStorage.removeItem，
    // 在 setItem 被全面 mock 抛错的情况下 removeItem 仍正常工作（未 mock）
    expect(localStorage.getItem('pipeline-messages')).toBeNull()
  })

  it('配额正常时不清理 pipeline-messages', () => {
    localStorage.setItem('pipeline-messages', '{"keep":"me"}')
    // setItem 正常工作（不 mock），addTab 成功写入
    useAgentTabStore.getState().addTab({
      id: 'sub-2',
      agentId: 'agent-y',
      agentName: '子Agent2',
      agentLevel: 2,
      path: ['主Agent', '子Agent2'],
      status: 'running',
      hasUnread: false,
      canClose: true,
    })
    // 正常路径不触发降级清理，pipeline-messages 保留
    expect(localStorage.getItem('pipeline-messages')).toBe('{"keep":"me"}')
  })
})
