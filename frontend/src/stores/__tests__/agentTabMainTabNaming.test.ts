// @feature: FP-0.2.可观测性 | @ci: frontend-test
/**
 * 主标签命名测试（2026-10-16 用户拍板：「主管道」系内部词汇不进用户可见面）。
 *
 * 契约：主标签名 = 会话标题（新会话标题默认即 agent 名「灵汐」，起名/改名后随
 * 会话走；改名同步见 sessionListStore.renameSession）；会话缓存未就绪回落
 * 「灵汐」（缺数据不是改名依据）；恢复分支把持久化旧名矫正到当前标题；
 * 子标签面包屑首段随会话标题。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { makeSessionFactory } from './helpers/agentTabTestUtils'
import type * as queryClientMod from '@/services/query/queryClient'
import type * as queryKeysMod from '@/services/query/queryKeys'
import type * as agentTabStoreMod from '@/stores/agentTabStore'
import type { Session } from '@/types/models'
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

describe('主标签命名 = 会话标题', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
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
  })

  it('新会话分支：主标签名/面包屑 = 会话标题', () => {
    seedSessions([makeSession({ title: '修复登录超时' })])

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('修复登录超时')
    expect(mainTab?.path).toEqual(['修复登录超时'])
  })

  it('会话缓存未就绪 → 回落「灵汐」（缺数据不是改名依据）', () => {
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const mainTab = useAgentTabStore.getState().tabs.find((t) => t.agentLevel === 1)
    expect(mainTab?.agentName).toBe('灵汐')
    expect(mainTab?.path).toEqual(['灵汐'])
  })

  it('恢复分支：持久化旧名/存量「主管道」矫正为当前会话标题', () => {
    seedSessions([makeSession({ title: '写第二十五章' })])
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

  it('恢复分支：会话缓存未就绪保持持久化名（不置空不猜名）', () => {
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

  it('openSubAgentTab：面包屑首段 = 会话标题', () => {
    seedSessions([makeSession({ title: '修复登录超时' })])
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
    expect(subTab?.path).toEqual(['修复登录超时', '子任务A'])
  })

  it('openSubAgentTab：会话缓存未就绪 → 面包屑首段回落「灵汐」', () => {
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
