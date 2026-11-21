// @feature: FP-0.2.可观测性 | @ci: frontend-test
/**
 * 主标签命名测试（2026-11-11 用户裁定：标签名只显示管道标题不显示会话标题）。
 *
 * 契约：主标签名 = 主管道标题（数据真值 = 注册表 run.title，ADR
 * 2026-10-27-pipeline-title-field；内核出生/改名传播同源）；主管道缺失或
 * 注册表无标题回落「灵汐」（缺数据不是改名依据）；恢复分支仅在注册表确有
 * 标题时矫正持久化旧名；子标签面包屑首段随主管道标题。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { makeSessionFactory } from './helpers/agentTabTestUtils'
import type * as queryClientMod from '@/services/query/queryClient'
import type * as queryKeysMod from '@/services/query/queryKeys'
import type * as agentTabStoreMod from '@/stores/agentTabStore'
import type { Session } from '@/types/models'
import type { PipelineRunInfo } from '@/types/pipeline'
import type { AgentTab } from '@/types/task'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))

// pipelineMessageStore 只 mock 外部依赖边界（网络/持久化），同激活重绑测试口径
vi.mock('@/stores/pipelineMessageStore', async () =>
  (await import('./helpers/pipelineStoreMockFactory')).makePipelineStoreMock(),
)

const SESSION_ID = 'sess-naming'
const MAIN_PID = 'pid-main-naming'
const MAIN_TAB_ID = `main-${SESSION_ID}`
const makeSession = makeSessionFactory(SESSION_ID, MAIN_PID)

/** 构造注册表运行行（title 可覆盖，缺省不带 title = 存量无键形态） */
function makeRun(overrides: Partial<PipelineRunInfo> = {}): PipelineRunInfo {
  return {
    run_id: MAIN_PID,
    pipeline_id: MAIN_PID,
    status: 'running',
    started_at: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

/** 模拟旧版本持久化的主标签（固定名「主管道」存量形态） */
function makePersistedMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: MAIN_TAB_ID,
    agentId: 'agentos',
    agentName: '主管道',
    agentLevel: 1,
    pipelineRunId: MAIN_PID,
    path: ['主管道'],
    status: 'running',
    hasUnread: false,
    canClose: false,
    messages: [],
    ...overrides,
  }
}

describe('主标签命名 = 主管道标题', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let seedSessions: (sessions: Session[]) => void
  let seedRuns: (runs: Record<string, PipelineRunInfo>) => void

  beforeEach(async () => {
    vi.resetModules()
    localStorage.clear()

    const { queryClient } = await import('@/services/query/queryClient')
    const { queryKeys } = await import('@/services/query/queryKeys')
    queryClient.clear()
    seedSessions = (sessions) => queryClient.setQueryData(queryKeys.sessions, sessions)
    seedRuns = (runs) => queryClient.setQueryData(queryKeys.pipelineRuns, runs)

    const mod = await import('@/stores/agentTabStore')
    useAgentTabStore = mod.useAgentTabStore
  })

  it('新会话分支：主标签名/面包屑 = 主管道标题（不是会话标题）', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({ [MAIN_PID]: makeRun({ title: '管道标题B' }) })

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('管道标题B')
    expect(mainTab?.path).toEqual(['管道标题B'])
  })

  it('注册表无主管道行 → 回落「灵汐」（缺数据不是改名依据）', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({})

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('灵汐')
    expect(mainTab?.path).toEqual(['灵汐'])
  })

  it('注册表行无 title 键（存量形态）→ 回落「灵汐」', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({ [MAIN_PID]: makeRun() })

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('灵汐')
  })

  it('恢复分支：持久化旧名/存量「主管道」矫正为注册表当前标题', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({ [MAIN_PID]: makeRun({ title: '写第二十五章' }) })
    localStorage.setItem(
      `agent-tabs-${SESSION_ID}`,
      JSON.stringify({
        tabs: [makePersistedMainTab()],
        activeTabId: MAIN_TAB_ID,
        savedAt: Date.now(),
      }),
    )

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('写第二十五章')
    expect(mainTab?.path).toEqual(['写第二十五章'])
  })

  it('恢复分支：注册表无标题保持持久化名（不置空不猜名）', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({})
    localStorage.setItem(
      `agent-tabs-${SESSION_ID}`,
      JSON.stringify({
        tabs: [makePersistedMainTab({ agentName: '旧名', path: ['旧名'] })],
        activeTabId: MAIN_TAB_ID,
        savedAt: Date.now(),
      }),
    )

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('旧名')
    expect(mainTab?.path).toEqual(['旧名'])
  })

  it('openSubAgentTab：面包屑首段 = 主管道标题', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({ [MAIN_PID]: makeRun({ title: '管道标题B' }) })
    useAgentTabStore.setState({ currentSessionId: SESSION_ID })
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    useAgentTabStore.getState().openSubAgentTab({
      agentId: 'coder',
      agentName: '子任务A',
      parentRecordId: 'pid-sub-1',
      agentLevel: 2,
      setActive: false,
      pipelineId: 'pid-sub-1',
    })

    const subTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 2)
    expect(subTab?.path).toEqual(['管道标题B', '子任务A'])
  })

  it('openSubAgentTab：注册表无标题 → 面包屑首段回落「灵汐」', () => {
    seedSessions([makeSession({ title: '会话标题A' })])
    seedRuns({})
    useAgentTabStore.setState({ currentSessionId: SESSION_ID })
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    useAgentTabStore.getState().openSubAgentTab({
      agentId: 'coder',
      agentName: '子任务A',
      parentRecordId: 'pid-sub-2',
      agentLevel: 2,
      setActive: false,
      pipelineId: 'pid-sub-2',
    })

    const subTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 2)
    expect(subTab?.path).toEqual(['灵汐', '子任务A'])
  })
})
