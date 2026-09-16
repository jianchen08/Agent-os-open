// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * AgentTabStore initSessionTabs 持久化绑定保全测试（BUG-28 续作）
 *
 * 契约（与 rebindMainTabToSession 同一不变量「缺数据不是改绑依据」）：
 * - 会话缓存未就绪/权威主管道缺失时，恢复分支必须保持持久化的主 Tab
 *   pipelineRunId（最后已知正确绑定），不得清洗为空——清空会让发送侧 pid
 *   解析恒空（真机 R67 取证：持久化 pipelineRunId=f36831f2ee89 完整但运行时
 *   activeTab.pipelineRunId 空，发送全局被受理协议拒）。
 * - 缓存就绪时权威主管道仍覆盖持久化值（既有语义不回退，每次激活由
 *   rebindMainTabToSession 矫正）。
 * - initSessionTabs 本身落盘：会话首次进入即有 agent-tabs 持久化键（真机
 *   R67 取证②：R66 会话无持久化键，缓存故障窗口内无 last-known-good 可恢复）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { makeSessionFactory } from './helpers/agentTabTestUtils'
import type * as agentTabStoreMod from '@/stores/agentTabStore'
import type { Session } from '@/types/models'
import type { AgentTab } from '@/types/task'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))

vi.mock('@/stores/pipelineMessageStore', async () =>
  (await import('./helpers/pipelineStoreMockFactory')).makePipelineStoreMock(),
)

const SESSION_ID = 'thread-a72ad5cd-1111'
const MAIN_TAB_ID = `main-${SESSION_ID}`
const PERSISTED_PID = 'f36831f2ee89'
const AUTHORITY_PID = 'aa11bb22cc33'

function makeMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: MAIN_TAB_ID,
    agentId: 'agentos',
    agentName: '主管道',
    agentLevel: 1,
    pipelineRunId: PERSISTED_PID,
    path: ['主管道'],
    status: 'running',
    hasUnread: false,
    canClose: false,
    messages: [],
    ...overrides,
  }
}

const makeSession = makeSessionFactory(SESSION_ID, AUTHORITY_PID)

function persistedTabsPayload() {
  return JSON.stringify({
    tabs: [makeMainTab()],
    activeTabId: MAIN_TAB_ID,
    pipelineTabMap: { [PERSISTED_PID]: MAIN_TAB_ID },
    savedAt: Date.now(),
  })
}

describe('AgentTabStore initSessionTabs 持久化绑定保全（BUG-28 恢复链断）', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let pipelineMock: {
    activatePipeline: ReturnType<typeof vi.fn>
    registerPipeline: ReturnType<typeof vi.fn>
    loadPipelineMessages: ReturnType<typeof vi.fn>
    activePipelineId: string | null
    pipelines: Record<string, unknown>
    messagesByPipeline: Record<string, unknown[]>
  }
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
    pipelineMock = (pm as unknown as { __pipelineMockState: typeof pipelineMock })
      .__pipelineMockState
    pipelineMock.activatePipeline.mockClear()
    pipelineMock.registerPipeline.mockClear()
    pipelineMock.loadPipelineMessages.mockClear()
    pipelineMock.pipelines = {}
    pipelineMock.messagesByPipeline = {}
    pipelineMock.activePipelineId = null
  })

  it('缓存未就绪（无会话缓存）：恢复保持持久化 pipelineRunId，不清洗为空', () => {
    localStorage.setItem(`agent-tabs-${SESSION_ID}`, persistedTabsPayload())

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, activeTabId, pipelineTabMap } = useAgentTabStore.getState()
    expect(activeTabId).toBe(MAIN_TAB_ID)
    const mainTab = tabs.find((t) => t.id === MAIN_TAB_ID)
    expect(mainTab?.pipelineRunId).toBe(PERSISTED_PID)
    // 映射随 Tab 重建：持久化主管道映射保留（发送侧串桶防线成员集依赖）
    expect(pipelineTabMap[PERSISTED_PID]).toBe(MAIN_TAB_ID)
  })

  it('缓存有会话但权威主管道缺失（activePipelineId 空+多管道）：保持持久化绑定', () => {
    seedSessions([makeSession({ activePipelineId: null, pipelineIds: ['pid-a', 'pid-b'] })])
    localStorage.setItem(`agent-tabs-${SESSION_ID}`, persistedTabsPayload())

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    const mainTab = tabs.find((t) => t.id === MAIN_TAB_ID)
    expect(mainTab?.pipelineRunId).toBe(PERSISTED_PID)
    expect(pipelineTabMap[PERSISTED_PID]).toBe(MAIN_TAB_ID)
  })

  it('缓存就绪（权威主管道在场）：权威值覆盖持久化值（既有语义不回退）', () => {
    seedSessions([makeSession()])
    localStorage.setItem(`agent-tabs-${SESSION_ID}`, persistedTabsPayload())

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    const mainTab = tabs.find((t) => t.id === MAIN_TAB_ID)
    expect(mainTab?.pipelineRunId).toBe(AUTHORITY_PID)
    expect(pipelineTabMap[AUTHORITY_PID]).toBe(MAIN_TAB_ID)
    expect(pipelineTabMap[PERSISTED_PID]).toBeUndefined()
  })

  it('initSessionTabs 即落盘：首次进入会话即有 agent-tabs 持久化键（取证②修复）', () => {
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const raw = localStorage.getItem(`agent-tabs-${SESSION_ID}`)
    expect(raw).not.toBeNull()
    const data = JSON.parse(raw!) as { tabs: AgentTab[]; activeTabId: string | null }
    expect(data.tabs.some((t) => t.id === MAIN_TAB_ID)).toBe(true)
    expect(data.activeTabId).toBe(MAIN_TAB_ID)
  })
})
