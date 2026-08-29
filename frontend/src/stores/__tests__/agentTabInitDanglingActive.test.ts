// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * AgentTabStore initSessionTabs 悬空 activeTabId 恢复测试
 *
 * 契约：localStorage 恢复时 saved.activeTabId 必须指向恢复面中真实存在的 Tab——
 * 悬空（保存后 Tab 被清理/数据残缺）时回落默认 Tab（主 Tab，缺失用首 Tab）。
 * 放任 activeTab 落空会跳过管道激活，而 pipelineMessageStore 跨会话单例，
 * activePipelineId 残留上一会话值会让消息视图/WS 绑定落到旧会话管道（跨会话
 * 消息桶错显）。无可激活管道（活跃 Tab 无 pipelineRunId）时清空残值而非保留。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Session } from '@/types/models'
import type { AgentTab } from '@/types/task'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))

// pipelineMessageStore 只 mock 外部依赖边界（网络/持久化）；setState 以对象合并
// 语义模拟（agentTabStore 悬空清空走 usePipelineMessageStore.setState），mock
// 状态经 __pipelineMockState 导出供断言（工厂内构造，避开 vi.mock 提升引用限制）。
vi.mock('@/stores/pipelineMessageStore', () => {
  const state = {
    activatePipeline: vi.fn(),
    registerPipeline: vi.fn(),
    loadPipelineMessages: vi.fn(() => Promise.resolve({ ok: true as const })),
    pipelines: {} as Record<string, unknown>,
    messagesByPipeline: {} as Record<string, unknown[]>,
    activePipelineId: null as string | null,
  }
  // 与真实 store 同语义：激活回写 activePipelineId（供清空/激活断言）
  state.activatePipeline = vi.fn((pipelineId: string) => {
    state.activePipelineId = pipelineId
  })
  const setState = vi.fn((partial: Record<string, unknown>) => {
    Object.assign(state, partial)
  })
  return {
    usePipelineMessageStore: { getState: () => state, setState },
    __pipelineMockState: state,
  }
})

const SESSION_ID = 'sess-1'
const MAIN_TAB_ID = `main-${SESSION_ID}`
const MAIN_PID = 'pid-main-auth'
const SUB_TAB_ID = 'sub-pid-sub-x'
const SUB_PID = 'pid-sub-x'
const DANGLING_TAB_ID = 'sub-pid-vanished'

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: SESSION_ID,
    title: '测试会话',
    agentId: 'agentos',
    activePipelineId: MAIN_PID,
    pipelineIds: [MAIN_PID],
    starred: false,
    pinned: false,
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  } as Session
}

function makeMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: MAIN_TAB_ID,
    agentId: 'agentos',
    agentName: '主Agent',
    agentLevel: 1,
    pipelineRunId: MAIN_PID,
    path: ['主Agent'],
    status: 'running',
    hasUnread: false,
    canClose: false,
    messages: [],
    ...overrides,
  }
}

function makeSubTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: SUB_TAB_ID,
    agentId: 'agent-sub',
    agentName: '子Agent',
    agentLevel: 2,
    parentRecordId: 'rec-sub-x',
    pipelineRunId: SUB_PID,
    path: ['主Agent', '子Agent'],
    status: 'running',
    hasUnread: false,
    canClose: true,
    messages: [],
    ...overrides,
  }
}

describe('AgentTabStore initSessionTabs 悬空 activeTabId 恢复', () => {
  interface PipelineMockState {
    activatePipeline: ReturnType<typeof vi.fn>
    registerPipeline: ReturnType<typeof vi.fn>
    loadPipelineMessages: ReturnType<typeof vi.fn>
    pipelines: Record<string, unknown>
    messagesByPipeline: Record<string, unknown[]>
    activePipelineId: string | null
  }
  let useAgentTabStore: typeof import('@/stores/agentTabStore').useAgentTabStore
  let pipelineMock: PipelineMockState
  let seedSessions: (sessions: Session[]) => void

  beforeEach(async () => {
    vi.resetModules()
    localStorage.clear()

    const { queryClient } = await import('@/services/query/queryClient')
    const { queryKeys } = await import('@/services/query/queryKeys')
    queryClient.clear()
    seedSessions = (sessions) => queryClient.setQueryData(queryKeys.sessions, sessions)

    const mod = await import('@/stores/agentTabStore')
    useAgentTabStore = mod.useAgentTabStore
    const pm = await import('@/stores/pipelineMessageStore')
    pipelineMock = (pm as unknown as { __pipelineMockState: PipelineMockState }).__pipelineMockState
    // vi.resetModules 不重置 vi.mock 工厂缓存——mock 状态须逐测试清零保证隔离
    pipelineMock.activatePipeline.mockClear()
    pipelineMock.registerPipeline.mockClear()
    pipelineMock.loadPipelineMessages.mockClear()
    pipelineMock.pipelines = {}
    pipelineMock.messagesByPipeline = {}
    pipelineMock.activePipelineId = null
  })

  /** 构造上一会话残留的 activePipelineId（模拟跨会话切入前的 store 状态） */
  function seedResidualActivePipeline(pid: string): void {
    pipelineMock.activePipelineId = pid
  }

  it('localStorage 悬空 activeTabId 恢复：回落主 Tab 并激活权威主管道', () => {
    seedSessions([makeSession()])
    seedResidualActivePipeline('pid-prev-session')
    localStorage.setItem(
      `agent-tabs-${SESSION_ID}`,
      JSON.stringify({
        tabs: [makeMainTab(), makeSubTab()],
        activeTabId: DANGLING_TAB_ID,
        savedAt: Date.now(),
      }),
    )

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { activeTabId, tabs } = useAgentTabStore.getState()
    // 回落主 Tab（默认 Tab），不再指向悬空 id
    expect(activeTabId).toBe(MAIN_TAB_ID)
    expect(tabs.some((t) => t.id === activeTabId)).toBe(true)
    // 管道已激活：不残留上一会话值，激活的是回落 Tab 绑定的权威主管道
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
    expect(pipelineMock.activatePipeline).not.toHaveBeenCalledWith('pid-prev-session')
    expect(pipelineMock.activePipelineId).toBe(MAIN_PID)
  })

  it('悬空 activeTabId 恢复：pipelineTabMap 只含恢复面真实绑定，不含悬空 id', () => {
    seedSessions([makeSession()])
    localStorage.setItem(
      `agent-tabs-${SESSION_ID}`,
      JSON.stringify({
        tabs: [makeMainTab(), makeSubTab()],
        activeTabId: DANGLING_TAB_ID,
        savedAt: Date.now(),
      }),
    )

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { pipelineTabMap } = useAgentTabStore.getState()
    expect(pipelineTabMap[MAIN_PID]).toBe(MAIN_TAB_ID)
    expect(pipelineTabMap[SUB_PID]).toBe(SUB_TAB_ID)
    expect(pipelineTabMap[DANGLING_TAB_ID]).toBeUndefined()
  })

  it('活跃 Tab 无 pipelineRunId（权威主管道缺失）：清空 activePipelineId 残值而非保留', () => {
    // 会话缓存未就绪（activePipelineId 缺失且管道数≠1）→ 主 Tab 无绑定可激活
    seedSessions([makeSession({ activePipelineId: null, pipelineIds: ['pid-a', 'pid-b'] })])
    seedResidualActivePipeline('pid-prev-session')
    localStorage.setItem(
      `agent-tabs-${SESSION_ID}`,
      JSON.stringify({
        tabs: [makeMainTab({ pipelineRunId: undefined }), makeSubTab()],
        activeTabId: MAIN_TAB_ID,
        savedAt: Date.now(),
      }),
    )

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { activeTabId } = useAgentTabStore.getState()
    expect(activeTabId).toBe(MAIN_TAB_ID)
    // 无激活目标：不得保留上一会话残值（跨会话消息桶错显源）
    expect(pipelineMock.activatePipeline).not.toHaveBeenCalled()
    expect(pipelineMock.activePipelineId).toBeNull()
  })
})
