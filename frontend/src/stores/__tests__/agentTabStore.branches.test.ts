/** @feature FP-T12 前端组件补测 | @ci frontend-test */
/**
 * AgentTabStore 分支补测（激活即重绑/悬空恢复/配额清理既有测试之外的缺失分支）：
 * - 持久化契约：落盘内容、24h 过期清除、损坏快照回退、配额降级清理范围、
 *   非配额错误放弃、无会话不落盘
 * - Tab 增删改查：addTab 重复 id 合并、removeTab 全回落路径、未读计数、
 *   状态更新别名、resetAllTabs、openSubTab、getActiveTab/getActiveTabMessages
 * - closeTab 保护与回落激活、switchToTab 异常中止、setActiveTab 子 Tab 加载
 * - loadTabMessages 并发防重/缺数据早退/404 与其他错误分流
 * - pipelineTabMap 注册查询、openSubAgentTab 已存在更新、initSessionTabs
 *   活跃子 Tab 懒加载
 */
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { makeSessionFactory, makeSubTabFactory, makeSubTabInputFactory } from './helpers/agentTabTestUtils'
import type * as agentTabStoreMod from '@/stores/agentTabStore'
import type { Session } from '@/types/models'
import type { AgentTab } from '@/types/task'
import type { loggers } from '@/utils/logger'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

// pipelineMessageStore 只 mock 外部依赖边界（网络/持久化），mock 状态经
// __pipelineMockState 导出供断言（工厂内构造，避开 vi.mock 提升引用限制）。
vi.mock('@/stores/pipelineMessageStore', async () => (await import('./helpers/pipelineStoreMockFactory')).makePipelineStoreMock())
const SESSION_ID = 'sess-1'
const MAIN_TAB_ID = `main-${SESSION_ID}`
const MAIN_PID = 'pid-main'
const SUB_TAB_ID = 'sub-pid-sub-x'
const SUB_PID = 'pid-sub-x'
const makeSession = makeSessionFactory(SESSION_ID, MAIN_PID)
const makeSubTab = makeSubTabFactory(SUB_TAB_ID, SUB_PID)
const makeSubTabInput = makeSubTabInputFactory(makeSubTab)
const STORAGE_KEY = `agent-tabs-${SESSION_ID}`

function makeMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
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

/** 保存一条指定 savedAt 的标签快照到 localStorage */
function seedSnapshot(tabs: AgentTab[], activeTabId: string, savedAt: number): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ tabs, activeTabId, savedAt }))
}

interface PipelineMockState {
    activatePipeline: ReturnType<typeof vi.fn>
    registerPipeline: ReturnType<typeof vi.fn>
    loadPipelineMessages: ReturnType<typeof vi.fn>
    getMessages: ReturnType<typeof vi.fn>
    pipelines: Record<string, unknown>
    messagesByPipeline: Record<string, unknown[]>
    activePipelineId: string | null
  }

/** 重置模块注册表/存储并取回 agentTab store + pipelineMock + sessions 播种器 */
async function importAgentTabEnv(): Promise<{
  useAgentTabStore: agentTabStoreMod.useAgentTabStore
  pipelineMock: PipelineMockState
  seedSessions: (sessions: Session[]) => void
}> {
  vi.resetModules()
  localStorage.clear()

  const { queryClient } = await import('@/services/query/queryClient')
  const { queryKeys } = await import('@/services/query/queryKeys')
  queryClient.clear()
  const seedSessions = (sessions: Session[]) => queryClient.setQueryData(queryKeys.sessions, sessions)

  const mod = await import('@/stores/agentTabStore')
  const pm = await import('@/stores/pipelineMessageStore')
  const pipelineMock = (pm as unknown as { __pipelineMockState: PipelineMockState }).__pipelineMockState
  return { useAgentTabStore: mod.useAgentTabStore, pipelineMock, seedSessions }
}

/** pipelineMock 状态清零——vi.resetModules 不重置 vi.mock 工厂缓存，须逐测试清零保证隔离 */
function clearPipelineMockState(pipelineMock: PipelineMockState): void {
  pipelineMock.activatePipeline.mockClear()
  pipelineMock.registerPipeline.mockClear()
  pipelineMock.getMessages.mockClear()
  pipelineMock.getMessages.mockImplementation(() => [])
  pipelineMock.loadPipelineMessages.mockClear()
  pipelineMock.loadPipelineMessages.mockImplementation(() => Promise.resolve({ ok: true as const }))
  pipelineMock.pipelines = {}
  pipelineMock.messagesByPipeline = {}
  pipelineMock.activePipelineId = null
}

/** 三组用例共用的模块重置环境：重置模块注册表/存储并装配 pipeline mock */
async function resetAgentTabEnv(): Promise<{
  useAgentTabStore: agentTabStoreMod.useAgentTabStore
  pipelineMock: PipelineMockState
  seedSessions: (sessions: Session[]) => void
}> {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const env = await importAgentTabEnv()
  clearPipelineMockState(env.pipelineMock)
  return env
}

describe('AgentTabStore 持久化契约', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let pipelineMock: PipelineMockState
  let seedSessions: (sessions: Session[]) => void

  beforeEach(async () => {
    ;({ useAgentTabStore, pipelineMock, seedSessions } = await resetAgentTabEnv())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('saveCurrentTabs 无 currentSessionId 时不落盘', () => {
    useAgentTabStore.getState().saveCurrentTabs()
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('落盘内容契约：tabs/activeTabId/pipelineTabMap/savedAt 齐全', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.getState().addTab(makeSubTabInput())
    useAgentTabStore.getState().registerPipelineTab(SUB_PID, SUB_TAB_ID)

    const raw = localStorage.getItem(STORAGE_KEY)
    expect(raw).toBeTruthy()
    const saved = JSON.parse(raw!)
    expect(saved.tabs.some((t: AgentTab) => t.id === SUB_TAB_ID)).toBe(true)
    expect(saved.activeTabId).toBe(MAIN_TAB_ID)
    expect(saved.pipelineTabMap[SUB_PID]).toBe(SUB_TAB_ID)
    expect(typeof saved.savedAt).toBe('number')
  })

  it('24 小时过期快照：清除旧内容并只建主 Tab（落盘为新鲜快照）', () => {
    seedSessions([makeSession()])
    seedSnapshot([makeMainTab(), makeSubTab()], MAIN_TAB_ID, Date.now() - 25 * 60 * 60 * 1000)

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, activeTabId } = useAgentTabStore.getState()
    expect(tabs).toHaveLength(1)
    expect(tabs[0].id).toBe(MAIN_TAB_ID)
    expect(activeTabId).toBe(MAIN_TAB_ID)
    // 过期快照内容必须消失：key 被清后 initSessionTabs 落盘的是重建态的新鲜
    // 快照（仅主 Tab、savedAt 为当前时刻）——陈旧 Tab 数据不得存活
    const raw = localStorage.getItem(STORAGE_KEY)
    expect(raw).not.toBeNull()
    const saved = JSON.parse(raw!) as { tabs: AgentTab[]; savedAt: number }
    expect(saved.tabs.some((t: AgentTab) => t.id === SUB_TAB_ID)).toBe(false)
    expect(saved.savedAt).toBeGreaterThan(Date.now() - 60_000)
  })

  it('损坏快照（非法 JSON）：回退新建主 Tab，不崩溃', () => {
    seedSessions([makeSession()])
    localStorage.setItem(STORAGE_KEY, '{not-valid-json')

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, activeTabId } = useAgentTabStore.getState()
    expect(tabs).toHaveLength(1)
    expect(tabs[0].id).toBe(MAIN_TAB_ID)
    expect(activeTabId).toBe(MAIN_TAB_ID)
  })

  it('快照缺主 Tab：恢复面补建主 Tab 置于首位、绑定以 session 为准，激活保持保存值', () => {
    seedSessions([makeSession()])
    seedSnapshot([makeSubTab()], SUB_TAB_ID, Date.now())

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { tabs, activeTabId, pipelineTabMap } = useAgentTabStore.getState()
    expect(tabs[0].id).toBe(MAIN_TAB_ID)
    expect(tabs[0].agentLevel).toBe(1)
    expect(tabs[0].pipelineRunId).toBe(MAIN_PID)
    expect(tabs.some((t) => t.id === SUB_TAB_ID)).toBe(true)
    // 保存的激活 Tab（子 Tab）仍在恢复面：保持激活，不被补建的主 Tab 抢占
    expect(activeTabId).toBe(SUB_TAB_ID)
    expect(pipelineTabMap[MAIN_PID]).toBe(MAIN_TAB_ID)
    expect(pipelineTabMap[SUB_PID]).toBe(SUB_TAB_ID)
  })

  it('配额不足：清理其他会话 agent-tabs 数据与 legacy key 后重试成功，当前会话保留', () => {
    seedSessions([makeSession()])
    localStorage.setItem(
      'agent-tabs-sess-old1',
      JSON.stringify({ tabs: [], activeTabId: null, savedAt: 1000 }),
    )
    localStorage.setItem('agent-tabs-sess-old2', '{corrupt') // 损坏数据也纳入清理
    localStorage.setItem(
      'agent-tabs-other',
      JSON.stringify({ tabs: [], activeTabId: null, savedAt: 500 }),
    )
    localStorage.setItem('pipeline-messages', '{"legacy":true}')
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    // 首次写入抛 QuotaExceededError，触发降级清理后重试（重试走真实 setItem）
    const realSetItem = localStorage.setItem.bind(localStorage)
    vi.spyOn(localStorage, 'setItem')
      .mockImplementationOnce(() => {
        throw new DOMException('quota exceeded', 'QuotaExceededError')
      })
      .mockImplementation(realSetItem)

    expect(() => useAgentTabStore.getState().addTab(makeSubTabInput())).not.toThrow()

    // 降级清理范围：其他会话 agent-tabs-*（含损坏 JSON 的 key）与 legacy key
    expect(localStorage.getItem('agent-tabs-sess-old1')).toBeNull()
    expect(localStorage.getItem('agent-tabs-sess-old2')).toBeNull()
    expect(localStorage.getItem('agent-tabs-other')).toBeNull()
    expect(localStorage.getItem('pipeline-messages')).toBeNull()
    // 重试成功：当前会话数据落盘
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.tabs.some((t: AgentTab) => t.id === SUB_TAB_ID)).toBe(true)
  })

  it('非配额错误：放弃保存且不触发降级清理', () => {
    seedSessions([makeSession()])
    localStorage.setItem('pipeline-messages', '{"keep":true}')
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('disk on fire')
    })

    expect(() => useAgentTabStore.getState().addTab(makeSubTabInput())).not.toThrow()
    // 非 Quota 错误走 warn 早退：legacy key 不被清理
    expect(localStorage.getItem('pipeline-messages')).toBe('{"keep":true}')
  })

  it('initSessionTabs 活跃 Tab 为子 Tab：懒注册管道并只加载该子 Tab 消息', () => {
    seedSessions([makeSession()])
    seedSnapshot([makeMainTab(), makeSubTab()], SUB_TAB_ID, Date.now())

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    const { activeTabId } = useAgentTabStore.getState()
    expect(activeTabId).toBe(SUB_TAB_ID)
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(SUB_PID)
    // 子 Tab 消息立即加载；主管道由 sessionListStore 负责加载，不在此拉取
    expect(pipelineMock.loadPipelineMessages).toHaveBeenCalledWith(SUB_PID, {
      threadId: SESSION_ID,
    })
    expect(pipelineMock.loadPipelineMessages).not.toHaveBeenCalledWith(MAIN_PID, expect.anything())
    expect(pipelineMock.registerPipeline).toHaveBeenCalledWith(
      expect.objectContaining({ pipelineId: SUB_PID, tabId: SUB_TAB_ID }),
    )
  })
})

describe('AgentTabStore Tab 增删改查', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let pipelineMock: PipelineMockState
  let seedSessions: (sessions: Session[]) => void

  beforeEach(async () => {
    ;({ useAgentTabStore, pipelineMock, seedSessions } = await resetAgentTabEnv())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  /** 播种会话+主 Tab+子 Tab，返回 store（unread=true 时子 Tab 带未读） */
  function seedSubTab(unread = false) {
    seedSessions([makeSession()])
    const store = useAgentTabStore.getState()
    store.initSessionTabs(SESSION_ID)
    store.addTab(makeSubTabInput())
    if (unread) store.updateTabUnread(SUB_TAB_ID, true)
    return store
  }

  it('addTab 重复 id：合并不新增，消息槽始终置空', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    useAgentTabStore.getState().addTab(makeSubTabInput({ status: 'running' }))
    useAgentTabStore.getState().addTab(makeSubTabInput({ status: 'completed' }))

    const { tabs, unreadCounts } = useAgentTabStore.getState()
    const subs = tabs.filter((t) => t.id === SUB_TAB_ID)
    expect(subs).toHaveLength(1)
    expect(subs[0].status).toBe('completed')
    expect(subs[0].messages).toEqual([])
    expect(unreadCounts[SUB_TAB_ID]).toBe(0)
  })

  it('removeTab：非激活 Tab 移除并联动清理未读与管道映射，落盘同步', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.getState().addTab(makeSubTabInput())
    useAgentTabStore.getState().registerPipelineTab(SUB_PID, SUB_TAB_ID)
    useAgentTabStore.getState().updateTabUnread(SUB_TAB_ID, true)

    useAgentTabStore.getState().removeTab(SUB_TAB_ID)

    const { tabs, unreadCounts, pipelineTabMap, activeTabId } = useAgentTabStore.getState()
    expect(tabs.some((t) => t.id === SUB_TAB_ID)).toBe(false)
    expect(unreadCounts[SUB_TAB_ID]).toBeUndefined()
    expect(pipelineTabMap[SUB_PID]).toBeUndefined()
    expect(activeTabId).toBe(MAIN_TAB_ID)
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.tabs.some((t: AgentTab) => t.id === SUB_TAB_ID)).toBe(false)
  })

  it('removeTab 删除激活 Tab：回落主 Tab', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.getState().addTab(makeSubTabInput())
    useAgentTabStore.getState().setActiveTab(SUB_TAB_ID)

    useAgentTabStore.getState().removeTab(SUB_TAB_ID)

    expect(useAgentTabStore.getState().activeTabId).toBe(MAIN_TAB_ID)
  })

  it('removeTab 删除激活 Tab 且无主 Tab：回落剩余首 Tab', () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeSubTab({ id: 'sub-a', pipelineRunId: 'pid-a' }), makeSubTab({ id: 'sub-b', pipelineRunId: 'pid-b' })],
      activeTabId: 'sub-a',
      unreadCounts: { 'sub-a': 1 },
      pipelineTabMap: { 'pid-a': 'sub-a', 'pid-b': 'sub-b' },
    })

    useAgentTabStore.getState().removeTab('sub-a')

    const { activeTabId, unreadCounts, pipelineTabMap } = useAgentTabStore.getState()
    expect(activeTabId).toBe('sub-b')
    expect(unreadCounts['sub-a']).toBeUndefined()
    expect(pipelineTabMap['pid-a']).toBeUndefined()
  })

  it('removeTab 清空全部 Tab：activeTabId 落 null', () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeSubTab({ id: 'sub-only', pipelineRunId: 'pid-only' })],
      activeTabId: 'sub-only',
      pipelineTabMap: { 'pid-only': 'sub-only' },
    })

    useAgentTabStore.getState().removeTab('sub-only')

    const { tabs, activeTabId } = useAgentTabStore.getState()
    expect(tabs).toHaveLength(0)
    expect(activeTabId).toBeNull()
  })

  it('removeTab 不存在的 id：状态不变', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    const before = useAgentTabStore.getState()

    useAgentTabStore.getState().removeTab('ghost')

    const after = useAgentTabStore.getState()
    expect(after.tabs).toHaveLength(before.tabs.length)
    expect(after.activeTabId).toBe(before.activeTabId)
  })

  it('updateTabStatus/markTabComplete/updateTab 写入状态，目标不存在时无副作用', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.getState().addTab(makeSubTabInput())

    const s = useAgentTabStore.getState()
    s.updateTabStatus(SUB_TAB_ID, 'failed')
    s.markTabComplete(SUB_TAB_ID)
    s.updateTab(SUB_TAB_ID, { agentName: '改名' })
    s.updateTab('ghost', { agentName: '幽灵' })
    s.updateTabStatus('ghost', 'completed')
    s.markTabComplete('ghost')

    const sub = useAgentTabStore.getState().tabs.find((t) => t.id === SUB_TAB_ID)
    expect(sub?.status).toBe('completed') // markTabComplete 覆盖在前
    expect(sub?.agentName).toBe('改名')
    expect(useAgentTabStore.getState().tabs.some((t) => t.id === 'ghost')).toBe(false)
  })

  it('updateTabUnread：hasUnread=true 递增、false 归零', () => {
    seedSubTab()

    useAgentTabStore.getState().updateTabUnread(SUB_TAB_ID, true)
    useAgentTabStore.getState().updateTabUnread(SUB_TAB_ID, true)
    expect(useAgentTabStore.getState().unreadCounts[SUB_TAB_ID]).toBe(2)
    expect(useAgentTabStore.getState().tabs.find((t) => t.id === SUB_TAB_ID)?.hasUnread).toBe(true)

    useAgentTabStore.getState().updateTabUnread(SUB_TAB_ID, false)
    expect(useAgentTabStore.getState().unreadCounts[SUB_TAB_ID]).toBe(0)
    expect(useAgentTabStore.getState().tabs.find((t) => t.id === SUB_TAB_ID)?.hasUnread).toBe(false)
  })

  it('clearUnread 别名：计数归零且 hasUnread 复位', () => {
    seedSubTab(true)

    useAgentTabStore.getState().clearUnread(SUB_TAB_ID)

    const { unreadCounts, tabs } = useAgentTabStore.getState()
    expect(unreadCounts[SUB_TAB_ID]).toBe(0)
    expect(tabs.find((t) => t.id === SUB_TAB_ID)?.hasUnread).toBe(false)
  })

  it('getActiveTab/getActiveTabMessages：命中与空态', () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeSubTab()],
      activeTabId: SUB_TAB_ID,
    })
    pipelineMock.getMessages.mockImplementation(() => [{ id: 'm-1' }])

    expect(useAgentTabStore.getState().getActiveTab()?.id).toBe(SUB_TAB_ID)
    expect(useAgentTabStore.getState().getActiveTabMessages()).toEqual([{ id: 'm-1' }])
  })

  it('getActiveTabMessages 空态：无激活/激活悬空/无管道绑定均返回空数组', () => {
    expect(useAgentTabStore.getState().getActiveTabMessages()).toEqual([])

    useAgentTabStore.setState({ currentSessionId: SESSION_ID, activeTabId: 'ghost' })
    expect(useAgentTabStore.getState().getActiveTabMessages()).toEqual([])

    useAgentTabStore.setState({
      tabs: [makeSubTab({ pipelineRunId: undefined })],
      activeTabId: SUB_TAB_ID,
    })
    expect(useAgentTabStore.getState().getActiveTabMessages()).toEqual([])
  })

  it('getActiveTab：无激活 Tab 返回 null', () => {
    expect(useAgentTabStore.getState().getActiveTab()).toBeNull()
  })

  it('resetAllTabs：清空运行态（不含 currentSessionId）', () => {
    seedSubTab(true)

    useAgentTabStore.getState().resetAllTabs()

    const s = useAgentTabStore.getState()
    expect(s.tabs).toEqual([])
    expect(s.activeTabId).toBeNull()
    expect(s.unreadCounts).toEqual({})
    expect(s.pipelineTabMap).toEqual({})
    expect(s.tabMessagesLoading).toEqual({})
    expect(s.currentSessionId).toBe(SESSION_ID)
  })

  it('openSubTab：新开强制 canClose，重复打开合并', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    useAgentTabStore.getState().openSubTab(makeSubTabInput({ canClose: false }))
    let sub = useAgentTabStore.getState().tabs.find((t) => t.id === SUB_TAB_ID)
    expect(sub?.canClose).toBe(true)

    useAgentTabStore.getState().openSubTab(makeSubTabInput({ status: 'waiting_input' }))
    const { tabs, unreadCounts } = useAgentTabStore.getState()
    expect(tabs.filter((t) => t.id === SUB_TAB_ID)).toHaveLength(1)
    sub = tabs.find((t) => t.id === SUB_TAB_ID)
    expect(sub?.status).toBe('waiting_input')
    expect(unreadCounts[SUB_TAB_ID]).toBe(0)
  })

  it('reorderTab：拖拽源移动到目标位置，落盘顺序同步', () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeMainTab(), makeSubTab({ id: 'sub-a', pipelineRunId: 'pid-a' }), makeSubTab({ id: 'sub-b', pipelineRunId: 'pid-b' })],
      activeTabId: MAIN_TAB_ID,
    })

    useAgentTabStore.getState().reorderTab('sub-b', MAIN_TAB_ID)

    const ids = useAgentTabStore.getState().tabs.map((t) => t.id)
    expect(ids).toEqual(['sub-b', MAIN_TAB_ID, 'sub-a'])
    // 集合不变、仅顺序变化（性质断言）
    expect([...ids].sort()).toEqual([MAIN_TAB_ID, 'sub-a', 'sub-b'].sort())
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!) as { tabs: AgentTab[] }
    expect(saved.tabs.map((t) => t.id)).toEqual(ids)
  })

  it('reorderTab 守卫：同签与未知签均不移动', () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeMainTab(), makeSubTab()],
      activeTabId: MAIN_TAB_ID,
    })
    const before = useAgentTabStore.getState().tabs.map((t) => t.id)

    useAgentTabStore.getState().reorderTab(MAIN_TAB_ID, MAIN_TAB_ID)
    useAgentTabStore.getState().reorderTab('ghost', MAIN_TAB_ID)
    useAgentTabStore.getState().reorderTab(MAIN_TAB_ID, 'ghost')

    expect(useAgentTabStore.getState().tabs.map((t) => t.id)).toEqual(before)
  })
})

describe('AgentTabStore closeTab/switchToTab/setActiveTab 边界', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let pipelineMock: PipelineMockState
  let seedSessions: (sessions: Session[]) => void

  beforeEach(async () => {
    ;({ useAgentTabStore, pipelineMock, seedSessions } = await resetAgentTabEnv())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  /** 播种会话+主 Tab+子 Tab 并激活子 Tab，清空 pipeline mock 调用记录 */
  function seedActiveSubTab() {
    seedSessions([makeSession()])
    const store = useAgentTabStore.getState()
    store.initSessionTabs(SESSION_ID)
    store.addTab(makeSubTabInput())
    store.setActiveTab(SUB_TAB_ID)
    pipelineMock.activatePipeline.mockClear()
    pipelineMock.loadPipelineMessages.mockClear()
  }

  it('closeTab 不存在的 Tab 与主 Tab 拒绝关闭：状态均不变', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    const before = useAgentTabStore.getState()

    useAgentTabStore.getState().closeTab('ghost')
    expect(useAgentTabStore.getState().tabs).toHaveLength(before.tabs.length)

    useAgentTabStore.getState().closeTab(MAIN_TAB_ID)
    const after = useAgentTabStore.getState()
    expect(after.tabs).toHaveLength(before.tabs.length)
    expect(after.activeTabId).toBe(before.activeTabId)
  })

  it('closeTab 激活子 Tab 回落主 Tab：绑定正确时激活主管道并补拉空桶', () => {
    seedActiveSubTab()

    useAgentTabStore.getState().closeTab(SUB_TAB_ID)

    const { activeTabId } = useAgentTabStore.getState()
    expect(activeTabId).toBe(MAIN_TAB_ID)
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
    // 本地桶为空：补拉主管道历史
    expect(pipelineMock.loadPipelineMessages).toHaveBeenCalledWith(MAIN_PID, {
      threadId: SESSION_ID,
    })
  })

  it('closeTab 回落主 Tab：本地桶非空时不重复补拉', () => {
    seedActiveSubTab()
    pipelineMock.messagesByPipeline[MAIN_PID] = [{ id: 'm-already' }]

    useAgentTabStore.getState().closeTab(SUB_TAB_ID)

    expect(useAgentTabStore.getState().activeTabId).toBe(MAIN_TAB_ID)
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(MAIN_PID)
    expect(pipelineMock.loadPipelineMessages).not.toHaveBeenCalled()
  })

  it('switchToTab 不存在的 Tab：早退且状态不变', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    pipelineMock.activatePipeline.mockClear()

    useAgentTabStore.getState().switchToTab('ghost')

    expect(useAgentTabStore.getState().activeTabId).toBe(MAIN_TAB_ID)
    expect(pipelineMock.activatePipeline).not.toHaveBeenCalled()
  })

  it('switchToTab 目标 Tab 无 pipelineRunId：中止切换，不激活不落盘激活态', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.setState({
      tabs: [makeMainTab(), makeSubTab({ pipelineRunId: undefined })],
    })
    pipelineMock.activatePipeline.mockClear()

    useAgentTabStore.getState().switchToTab(SUB_TAB_ID)

    expect(useAgentTabStore.getState().activeTabId).toBe(MAIN_TAB_ID)
    expect(pipelineMock.activatePipeline).not.toHaveBeenCalled()
  })

  it('setActiveTab 切到子 Tab：触发该子 Tab 消息加载', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    useAgentTabStore.getState().addTab(makeSubTabInput())
    pipelineMock.loadPipelineMessages.mockClear()

    useAgentTabStore.getState().setActiveTab(SUB_TAB_ID)

    expect(useAgentTabStore.getState().activeTabId).toBe(SUB_TAB_ID)
    expect(pipelineMock.loadPipelineMessages).toHaveBeenCalledWith(SUB_PID, {
      threadId: SESSION_ID,
    })
  })

  it('setActiveTab 无会话上下文：不重绑不激活主管道，不落盘', () => {
    useAgentTabStore.setState({ tabs: [makeMainTab()] })
    pipelineMock.activatePipeline.mockClear()

    useAgentTabStore.getState().setActiveTab(MAIN_TAB_ID)

    expect(useAgentTabStore.getState().activeTabId).toBe(MAIN_TAB_ID)
    expect(pipelineMock.activatePipeline).not.toHaveBeenCalled()
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })
})

describe('AgentTabStore loadTabMessages 与管道映射', () => {
  let useAgentTabStore: agentTabStoreMod.useAgentTabStore
  let pipelineMock: PipelineMockState
  let seedSessions: (sessions: Session[]) => void
  let loggers: loggers

  const seedReady = () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeSubTab()],
      activeTabId: SUB_TAB_ID,
    })
  }

  beforeEach(async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
    ;({ useAgentTabStore, pipelineMock, seedSessions } = await importAgentTabEnv())
    const loggerMod = await import('@/utils/logger')
    loggers = loggerMod.loggers
    clearPipelineMockState(pipelineMock)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('并发防重：加载中的 Tab 不重复发起加载', async () => {
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeSubTab()],
      tabMessagesLoading: { [SUB_TAB_ID]: true },
    })

    await useAgentTabStore.getState().loadTabMessages(SUB_TAB_ID)

    expect(pipelineMock.loadPipelineMessages).not.toHaveBeenCalled()
    expect(useAgentTabStore.getState().tabMessagesLoading[SUB_TAB_ID]).toBe(true)
  })

  it('缺数据早退：Tab 不存在 / 无会话 / 无 pipelineRunId 均不发起加载', async () => {
    // Tab 不存在
    await useAgentTabStore.getState().loadTabMessages('ghost')
    expect(pipelineMock.loadPipelineMessages).not.toHaveBeenCalled()

    // 无 currentSessionId
    useAgentTabStore.setState({ currentSessionId: null, tabs: [makeSubTab()] })
    await useAgentTabStore.getState().loadTabMessages(SUB_TAB_ID)
    expect(pipelineMock.loadPipelineMessages).not.toHaveBeenCalled()

    // Tab 无 pipelineRunId
    useAgentTabStore.setState({
      currentSessionId: SESSION_ID,
      tabs: [makeSubTab({ pipelineRunId: undefined })],
    })
    await useAgentTabStore.getState().loadTabMessages(SUB_TAB_ID)
    expect(pipelineMock.loadPipelineMessages).not.toHaveBeenCalled()
    expect(console.error).toHaveBeenCalled()
  })

  it('404 静默：走 debug 留痕不报错，完成后恢复加载位并激活管道', async () => {
    seedReady()
    pipelineMock.loadPipelineMessages.mockResolvedValue({
      ok: false as const,
      error: { response: { status: 404 } },
    })
    // 已注册的管道不再重复注册
    pipelineMock.pipelines[SUB_PID] = { pipelineId: SUB_PID }

    await useAgentTabStore.getState().loadTabMessages(SUB_TAB_ID)

    expect(loggers.sessionStore.debug).toHaveBeenCalled()
    expect(console.error).not.toHaveBeenCalled()
    expect(pipelineMock.registerPipeline).not.toHaveBeenCalled()
    expect(pipelineMock.activatePipeline).toHaveBeenCalledWith(SUB_PID)
    expect(useAgentTabStore.getState().tabMessagesLoading[SUB_TAB_ID]).toBe(false)
  })

  it('非 404 错误：console.error 留痕，未注册的管道先注册', async () => {
    seedReady()
    pipelineMock.loadPipelineMessages.mockResolvedValue({
      ok: false as const,
      error: new Error('backend exploded'),
    })

    await useAgentTabStore.getState().loadTabMessages(SUB_TAB_ID)

    expect(console.error).toHaveBeenCalled()
    expect(pipelineMock.registerPipeline).toHaveBeenCalledWith(
      expect.objectContaining({ pipelineId: SUB_PID, tabId: SUB_TAB_ID }),
    )
    expect(useAgentTabStore.getState().tabMessagesLoading[SUB_TAB_ID]).toBe(false)
  })

  it('registerPipelineTab/getTabIdByPipeline：注册可查、未注册返回 undefined、落盘同步', () => {
    useAgentTabStore.setState({ currentSessionId: SESSION_ID })

    useAgentTabStore.getState().registerPipelineTab(SUB_PID, SUB_TAB_ID)

    expect(useAgentTabStore.getState().getTabIdByPipeline(SUB_PID)).toBe(SUB_TAB_ID)
    expect(useAgentTabStore.getState().getTabIdByPipeline('pid-unknown')).toBeUndefined()
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.pipelineTabMap[SUB_PID]).toBe(SUB_TAB_ID)
  })

  it('openSubAgentTab 已存在：换绑 pipelineId 时迁移映射', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    const base = {
      agentId: 'agent-a',
      agentName: '子任务A',
      parentRecordId: 'rec-1',
      agentLevel: 2 as const,
    }

    useAgentTabStore.getState().openSubAgentTab({ ...base, pipelineId: 'pid-a' })
    expect(useAgentTabStore.getState().getTabIdByPipeline('pid-a')).toBe('sub-rec-1')

    useAgentTabStore.getState().openSubAgentTab({ ...base, pipelineId: 'pid-b' })

    const { tabs, pipelineTabMap } = useAgentTabStore.getState()
    const tab = tabs.find((t) => t.id === 'sub-rec-1')
    expect(tab?.pipelineRunId).toBe('pid-b')
    expect(pipelineTabMap['pid-b']).toBe('sub-rec-1')
    expect(pipelineTabMap['pid-a']).toBeUndefined()
  })

  it('openSubAgentTab 已存在且未传 pipelineId/taskId/agentLevel：Tab 旧字段保留', () => {
    seedSessions([makeSession()])
    useAgentTabStore.getState().initSessionTabs(SESSION_ID)
    const base = {
      agentId: 'agent-a',
      agentName: '子任务A',
      parentRecordId: 'rec-1',
      agentLevel: 2 as const,
      taskId: 'task-1',
    }
    useAgentTabStore.getState().openSubAgentTab({ ...base, pipelineId: 'pid-a' })

    useAgentTabStore.getState().openSubAgentTab({ ...base, taskId: undefined, agentLevel: undefined })

    const { tabs } = useAgentTabStore.getState()
    const tab = tabs.find((t) => t.id === 'sub-rec-1')
    // 未传字段落回旧值（|| 兜底），Tab 绑定不漂移
    expect(tab?.pipelineRunId).toBe('pid-a')
    expect(tab?.taskId).toBe('task-1')
    expect(tab?.agentLevel).toBe(2)
  })
})
