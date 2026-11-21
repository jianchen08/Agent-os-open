/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * 对话流标签关签语义（与工作区标签栏对齐）
 *
 * 契约（对齐依据 = utils/tabCloseSemantics 与 layoutModeStore 的既有批量关签语义）：
 * - 不可关签 = 固定签（isPinned）∪ 业务禁关签（主对话签 canClose=false）；
 * - closeOtherTabs：保留基准签 + 不可关签，其余全关；
 * - closeAllTabs：仅保留不可关签；
 * - 关签连带清理 unreadCounts / pipelineTabMap，活跃位被关时回落主 Tab；
 * - 固定态可切换并随标签一起持久化（刷新后固定签仍受保护）；
 * - 固定签不可单签关闭（closeTab 守卫走同一份判定）。
 *
 * 测试策略：真实 agentTabStore + 真实 tabCloseSemantics，仅 mock 外部依赖边界
 * （会话 API / 日志 / 管道消息 store，后者负责网络与持久化）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  makeSessionFactory,
  makeSubTabFactory,
  makeSubTabInputFactory,
} from './helpers/agentTabTestUtils'
import type * as agentTabStoreMod from '@/stores/agentTabStore'
import type { Session } from '@/types/models'
import type { AgentTab } from '@/types/task'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

vi.mock('@/stores/pipelineMessageStore', async () =>
  (await import('./helpers/pipelineStoreMockFactory')).makePipelineStoreMock(),
)

const SESSION_ID = 'sess-parity'
const MAIN_TAB_ID = `main-${SESSION_ID}`
const MAIN_PID = 'pid-main'
const STORAGE_KEY = `agent-tabs-${SESSION_ID}`

const makeSession = makeSessionFactory(SESSION_ID, MAIN_PID)
const makeSubTab = makeSubTabFactory('sub-rec-a', 'pid-sub-a')
const makeSubTabInput = makeSubTabInputFactory(makeSubTab)

/** 重置模块注册表、存储与会话缓存，取回干净 store */
async function resetEnv(): Promise<{
  useAgentTabStore: agentTabStoreMod.useAgentTabStore
  seedSessions: (sessions: Session[]) => void
}> {
  vi.resetModules()
  localStorage.clear()
  const { queryClient } = await import('@/services/query/queryClient')
  const { queryKeys } = await import('@/services/query/queryKeys')
  queryClient.clear()
  const seedSessions = (sessions: Session[]) =>
    queryClient.setQueryData(queryKeys.sessions, sessions)
  const mod = await import('@/stores/agentTabStore')
  return { useAgentTabStore: mod.useAgentTabStore, seedSessions }
}

/**
 * 播种：主对话签 + 两个子签，激活 sub-b；固定态最后经 togglePinTab 真实通道
 * 设置（该通道自带落盘），故持久化断言看到的就是最终态。
 */
function seedThreeTabs(useAgentTabStore: agentTabStoreMod.useAgentTabStore): void {
  const store = useAgentTabStore.getState()
  store.initSessionTabs(SESSION_ID)
  store.addTab(makeSubTabInput({ id: 'sub-a', parentRecordId: 'rec-a', pipelineRunId: 'pid-a' }))
  store.addTab(makeSubTabInput({ id: 'sub-b', parentRecordId: 'rec-b', pipelineRunId: 'pid-b' }))
  useAgentTabStore.setState({
    activeTabId: 'sub-b',
    unreadCounts: { 'sub-a': 2, 'sub-b': 1 },
    pipelineTabMap: { [MAIN_PID]: MAIN_TAB_ID, 'pid-a': 'sub-a', 'pid-b': 'sub-b' },
  })
  useAgentTabStore.getState().togglePinTab('sub-b')
}

function tabIds(useAgentTabStore: agentTabStoreMod.useAgentTabStore): string[] {
  return useAgentTabStore.getState().tabs.map((t) => t.id)
}

describe('AgentTabStore 关签语义对齐（固定签 / 批量关签 / 活跃位回落）', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let seedSessions: (sessions: Session[]) => void

  beforeEach(async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
    ;({ useAgentTabStore, seedSessions } = await resetEnv())
    seedSessions([makeSession()])
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('closeOtherTabs：保留基准签 + 固定签 + 主对话签，其余全关', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.getState().closeOtherTabs('sub-a')

    // sub-b 虽非基准签，但已固定 → 不受批量关签影响
    expect(tabIds(useAgentTabStore).sort()).toEqual([MAIN_TAB_ID, 'sub-a', 'sub-b'].sort())
  })

  it('closeOtherTabs：非固定签被关，映射与未读同步清理', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.getState().closeOtherTabs('sub-a')

    const state = useAgentTabStore.getState()
    expect(state.pipelineTabMap['pid-a']).toBe('sub-a')
    expect(state.pipelineTabMap['pid-b']).toBe('sub-b')
    expect(state.unreadCounts['sub-b']).toBe(1)
  })

  it('closeAllTabs：仅保留固定签与主对话签（基准签不特殊）', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.getState().closeAllTabs()

    expect(tabIds(useAgentTabStore).sort()).toEqual([MAIN_TAB_ID, 'sub-b'].sort())
  })

  it('活跃签被批量关掉 → 回落主对话签（不留悬空 activeTabId）', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.setState({
      tabs: useAgentTabStore.getState().tabs.map((t) => ({ ...t, isPinned: false })),
    })
    useAgentTabStore.getState().closeAllTabs()

    expect(tabIds(useAgentTabStore)).toEqual([MAIN_TAB_ID])
    expect(useAgentTabStore.getState().activeTabId).toBe(MAIN_TAB_ID)
  })

  it('清理后映射表/未读表只保留在位签（无孤儿键）', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.getState().closeAllTabs()

    const state = useAgentTabStore.getState()
    expect(
      Object.values(state.pipelineTabMap).every((tabId) =>
        tabIds(useAgentTabStore).includes(tabId),
      ),
    ).toBe(true)
    expect(
      Object.keys(state.unreadCounts).every((tabId) => tabIds(useAgentTabStore).includes(tabId)),
    ).toBe(true)
  })

  it('togglePinTab：子签可固定/取消固定', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.getState().togglePinTab('sub-a')
    expect(useAgentTabStore.getState().tabs.find((t) => t.id === 'sub-a')?.isPinned).toBe(true)

    useAgentTabStore.getState().togglePinTab('sub-a')
    expect(useAgentTabStore.getState().tabs.find((t) => t.id === 'sub-a')?.isPinned).toBe(false)
  })

  it('togglePinTab：主对话签（业务禁关签）不参与固定——固定对它没有意义', () => {
    seedThreeTabs(useAgentTabStore)
    const before = useAgentTabStore.getState().tabs.find((t) => t.id === MAIN_TAB_ID)?.isPinned
    useAgentTabStore.getState().togglePinTab(MAIN_TAB_ID)
    expect(useAgentTabStore.getState().tabs.find((t) => t.id === MAIN_TAB_ID)?.isPinned).toBe(
      before,
    )
  })

  it('固定态随标签持久化（刷新后固定签仍受批量关签保护）', () => {
    seedThreeTabs(useAgentTabStore)
    const savedRaw = localStorage.getItem(STORAGE_KEY)
    const saved = JSON.parse(savedRaw as string) as { tabs: AgentTab[] }
    expect(saved.tabs.find((t) => t.id === 'sub-b')?.isPinned).toBe(true)
    expect(saved.tabs.find((t) => t.id === 'sub-a')?.isPinned).toBeFalsy()
  })

  it('固定签不可单签关闭（closeTab 守卫与批量关签同一判定）', () => {
    seedThreeTabs(useAgentTabStore)
    useAgentTabStore.getState().closeTab('sub-b')
    expect(tabIds(useAgentTabStore)).toContain('sub-b')

    // 取消固定后同一入口即可关闭——守卫不是死路
    useAgentTabStore.getState().togglePinTab('sub-b')
    useAgentTabStore.getState().closeTab('sub-b')
    expect(tabIds(useAgentTabStore)).not.toContain('sub-b')
  })

  it('全部签都不可关时批量关签为 no-op（幂等，不抛错）', () => {
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.setState((state) => ({
      tabs: state.tabs.map((t) => ({ ...t, isPinned: true })),
    }))
    const before = tabIds(useAgentTabStore)

    expect(() => useAgentTabStore.getState().closeAllTabs()).not.toThrow()
    expect(tabIds(useAgentTabStore)).toEqual(before)
  })
})
