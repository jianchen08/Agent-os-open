// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * AgentTabStore 激活即重绑测试
 *
 * 契约：主 Tab（agentLevel===1）的 pipelineRunId 在每次激活时以 session 权威
 * 主管道（active_pipeline_id 解析，mainPipelineIdOf）矫正。残留绑定（localStorage
 * 持久化的子任务/其他会话管道 id）会让会话轮次 WS 事件落进不显示的桶、发送侧
 * 把残留 id 发给内核（内核告警「pipeline_id 不属于该 thread」）——重绑发生在
 * initSessionTabs（恢复分支）与每次主 Tab 激活（switchToTab/setActiveTab/
 * closeTab 回落），不再只依赖整页刷新。
 *
 * 防回归面：
 * - 子 Tab 不参与重绑（子任务/管道查看走 sub-tab，不污染主 Tab）
 * - 权威主管道缺失时保持原绑定（缺数据不是改绑依据，不置空、不猜位）
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Session } from '@/types/models'
import type { AgentTab } from '@/types/task'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))

// pipelineMessageStore 只 mock 外部依赖边界（网络/持久化），mock 状态经
// __pipelineMockState 导出供断言（工厂内构造，避开 vi.mock 提升引用限制）。
vi.mock('@/stores/pipelineMessageStore', () => {
  const state = {
    activatePipeline: vi.fn(),
    registerPipeline: vi.fn(),
    loadPipelineMessages: vi.fn(() => Promise.resolve({ ok: true as const })),
    pipelines: {} as Record<string, unknown>,
    messagesByPipeline: {} as Record<string, unknown[]>,
  }
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
const STALE_SUB_PID = 'pid-sub-stale'
const STALE_OTHER_SESSION_PID = 'pid-from-other-session'
const SUB_TAB_ID = 'sub-pid-sub-x'
const SUB_PID = 'pid-sub-x'

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

describe('AgentTabStore 激活即重绑', () => {
  interface PipelineMockState {
    activatePipeline: ReturnType<typeof vi.fn>
    registerPipeline: ReturnType<typeof vi.fn>
    loadPipelineMessages: ReturnType<typeof vi.fn>
    pipelines: Record<string, unknown>
    messagesByPipeline: Record<string, unknown[]>
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
  })

  /** 构造带残留绑定的 store 状态：主 Tab 持 stalePid、可选挂一个子 Tab */
  function seedStaleMainBinding(stalePid: string, activeTabId: string): void {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [
        makeMainTab({ pipelineRunId: stalePid }),
        makeSubTab(),
      ],
      activeTabId,
      pipelineTabMap: {
        [stalePid]: MAIN_TAB_ID,
        [SUB_PID]: SUB_TAB_ID,
      },
    })
  }

  it('initSessionTabs：localStorage 残留主 Tab pipelineRunId=子任务管道 → 重绑为 session 权威主管道', () => {
    seedSessions([makeSession()])
    localStorage.setItem(
      `agent-tabs-${SESSION_ID}`,
      JSON.stringify({
        tabs: [makeMainTab({ pipelineRunId: STALE_SUB_PID })],
        activeTabId: MAIN_TAB_ID,
        savedAt: Date.now(),
      }),
    )

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    const mainTab = tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.pipelineRunId).toBe(MAIN_PID)
    // 映射随重绑修正：权威主管道指向主 Tab，残留值不再指向主 Tab
    expect(pipelineTabMap[MAIN_PID]).toBe(MAIN_TAB_ID)
    expect(pipelineTabMap[STALE_SUB_PID]).toBeUndefined()
    // 激活管道与重绑后的绑定一致（视图消息桶按 activePipelineId 取）
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
  })

  it.each([STALE_SUB_PID, STALE_OTHER_SESSION_PID])(
    'switchToTab 返回主聊天：主 Tab 残留绑定 %s 重绑为权威主管道',
    (stalePid) => {
      seedSessions([makeSession()])
      useAgentTabStore.getState().initSessionTabs(SESSION_ID)
      seedStaleMainBinding(stalePid, SUB_TAB_ID)
      pipelineMock.activatePipeline.mockClear()

      useAgentTabStore.getState().switchToTab(MAIN_TAB_ID)

      const { tabs, pipelineTabMap, activeTabId } = useAgentTabStore.getState()
      const mainTab = tabs.find((t) => t.agentLevel === 1)
      expect(mainTab?.pipelineRunId).toBe(MAIN_PID)
      expect(activeTabId).toBe(MAIN_TAB_ID)
      expect(pipelineTabMap[MAIN_PID]).toBe(MAIN_TAB_ID)
      expect(pipelineTabMap[stalePid]).toBeUndefined()
      // 激活按矫正后的权威主管道，而非残留值
      expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
      expect(pipelineMock.activatePipeline).not.toHaveBeenCalledWith(stalePid)
    },
  )

  it('switchToTab 重复激活已冻结的主 Tab（activeTabId 已是主 Tab）→ 重绑仍生效', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    seedStaleMainBinding(STALE_SUB_PID, MAIN_TAB_ID)
    pipelineMock.activatePipeline.mockClear()

    useAgentTabStore.getState().switchToTab(MAIN_TAB_ID)

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    expect(tabs.find((t) => t.agentLevel === 1)?.pipelineRunId).toBe(MAIN_PID)
    expect(pipelineTabMap[STALE_SUB_PID]).toBeUndefined()
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
  })

  it('switchToTab 绑定已正确时：不改绑（无额外重绑写），仍按当前绑定激活', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    seedStaleMainBinding(MAIN_PID, SUB_TAB_ID)
    pipelineMock.activatePipeline.mockClear()
    const before = useAgentTabStore.getState()

    useAgentTabStore.getState().switchToTab(MAIN_TAB_ID)

    const after = useAgentTabStore.getState()
    expect(after.tabs.find((t) => t.agentLevel === 1)?.pipelineRunId).toBe(MAIN_PID)
    // 未发生改绑：映射引用不变（重绑会换新对象），激活只来自 switchToTab 本身一次
    expect(after.pipelineTabMap).toBe(before.pipelineTabMap)
    expect(pipelineMock.activatePipeline).toHaveBeenCalledTimes(1)
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
  })

  it('closeTab 回落主 Tab：残留绑定重绑为权威主管道', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    seedStaleMainBinding(STALE_SUB_PID, SUB_TAB_ID)
    pipelineMock.activatePipeline.mockClear()

    useAgentTabStore.getState().closeTab(SUB_TAB_ID)

    const { tabs, pipelineTabMap, activeTabId } = useAgentTabStore.getState()
    expect(activeTabId).toBe(MAIN_TAB_ID)
    expect(tabs.find((t) => t.agentLevel === 1)?.pipelineRunId).toBe(MAIN_PID)
    expect(pipelineTabMap[STALE_SUB_PID]).toBeUndefined()
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
  })

  it('setActiveTab 主 Tab：残留绑定重绑为权威主管道', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    seedStaleMainBinding(STALE_SUB_PID, SUB_TAB_ID)
    pipelineMock.activatePipeline.mockClear()

    useAgentTabStore.getState().setActiveTab(MAIN_TAB_ID)

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    expect(tabs.find((t) => t.agentLevel === 1)?.pipelineRunId).toBe(MAIN_PID)
    expect(pipelineTabMap[STALE_SUB_PID]).toBeUndefined()
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
  })

  it('权威主管道缺失（activePipelineId 空且管道数≠1）：保持原绑定，不置空不猜位', () => {
    seedSessions([makeSession({ activePipelineId: null, pipelineIds: ['pid-a', 'pid-b'] })])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    seedStaleMainBinding(STALE_SUB_PID, SUB_TAB_ID)

    useAgentTabStore.getState().switchToTab(MAIN_TAB_ID)

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    // 缺权威数据不是改绑依据：绑定原样保留（fail-closed 交由发送侧拒发/内核守卫）
    expect(tabs.find((t) => t.agentLevel === 1)?.pipelineRunId).toBe(STALE_SUB_PID)
    expect(pipelineTabMap[STALE_SUB_PID]).toBe(MAIN_TAB_ID)
  })

  it('子任务查看走 sub-tab：openSubAgentTab 激活子管道不污染主 Tab 绑定', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    useAgentTabStore.getState().openSubAgentTab({
      agentId: 'agent-sub-2',
      agentName: '子任务B',
      parentRecordId: 'pid-sub-2',
      agentLevel: 2,
      setActive: true,
      pipelineId: 'pid-sub-2',
    })

    const { tabs, activeTabId } = useAgentTabStore.getState()
    expect(tabs.find((t) => t.agentLevel === 1)?.pipelineRunId).toBe(MAIN_PID)
    expect(activeTabId).toBe('sub-pid-sub-2')
    expect(tabs.find((t) => t.id === 'sub-pid-sub-2')?.pipelineRunId).toBe('pid-sub-2')
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith('pid-sub-2')
  })
})
