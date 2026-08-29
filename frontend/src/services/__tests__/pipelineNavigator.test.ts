// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * pipelineNavigator 全局管道导航服务测试
 *
 * 覆盖 findPipelineLocation 三级查找 + navigateToPipeline 全路径：
 * 1. 内存缓存命中（pipelineSessionMap）
 * 2. 缓存与权威会话列表不一致 → 修正缓存（registerPipeline）
 * 3. 会话列表权威命中（readSessions）
 * 4. 缓存与会话列表都未命中 → forceReloadSessions 强制重拉兜底（绕过陈旧缓存）
 * 5. 兜底重查仍无 → null / false
 * 6. 无活跃会话 → false
 * 7. 当前 Tab 已是目标管道 → 快速返回 true
 * 8. 跨会话切换（saveCurrentTabs + setActiveSession + initSessionTabs）
 * 9. 目标会话已不存在 → 中止返回 false
 * 10. 已有标签匹配 pipelineRunId → switchToTab
 * 11. 主管道 → main-{sessionId} 主标签 switchToTab
 * 12. 子管道 → registerPipeline + openSubAgentTab + loadTabMessages
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'

const { mockPipelineStore, mockTabStore, mockSessionListStore, mockSessionStore } = vi.hoisted(
  () => ({
    mockPipelineStore: {
      pipelineSessionMap: {},
      pipelines: {},
      registerPipeline: vi.fn(),
    },
    mockTabStore: {
      tabs: [],
      activeTabId: null,
      pipelineTabMap: {},
      saveCurrentTabs: vi.fn(),
      initSessionTabs: vi.fn(),
      switchToTab: vi.fn(),
      openSubAgentTab: vi.fn(),
      loadTabMessages: vi.fn().mockResolvedValue(undefined),
    },
    mockSessionListStore: {
      setActiveSession: vi.fn().mockResolvedValue(undefined),
    },
    mockSessionStore: {
      activeSessionId: null,
    },
  }),
)

vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: { getState: () => mockPipelineStore },
}))

vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: { getState: () => mockTabStore },
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: { getState: () => mockSessionStore },
}))

vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => mockSessionListStore },
}))

// readSessions 由 mock 控制；forceReloadSessions 模拟"强制重拉并写回缓存"：
// 首次调用时把 fetchedSessions 灌入 mockSessions（即 query cache），
// 之后的 readSessions 就能读到——与真实 forceReloadSessions 语义一致
// （导航兜底必须绕过缓存，见 pipelineNavigator 第三级注释）。
let mockSessions: any[] = []
let fetchedSessions: any[] = []
let mockEnsureThrows = false
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: () => mockSessions,
  forceReloadSessions: async () => {
    if (mockEnsureThrows) throw new Error('fetch failed')
    mockSessions = fetchedSessions // 拉取成功 → 回填缓存
    return mockSessions
  },
}))

vi.mock('@/utils/logger', () => ({
  loggers: { websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

import { findPipelineLocation, navigateToPipeline } from '../pipelineNavigator'

const SESSION_A = 'session-a'
const SESSION_B = 'session-b'
const MAIN_PIPE_A = 'main-pipe-a'

function resetAll() {
  mockSessions = []
  fetchedSessions = []
  mockEnsureThrows = false
  useNotificationStore.setState({
    notifications: [],
    isPanelOpen: false,
    activeBlockingNotification: null,
  })
  mockPipelineStore.pipelineSessionMap = {}
  mockPipelineStore.pipelines = {}
  mockPipelineStore.registerPipeline.mockClear()
  mockTabStore.tabs = []
  mockTabStore.activeTabId = null
  mockTabStore.pipelineTabMap = {}
  mockTabStore.saveCurrentTabs.mockClear()
  mockTabStore.initSessionTabs.mockClear()
  mockTabStore.switchToTab.mockClear()
  mockTabStore.openSubAgentTab.mockClear()
  mockTabStore.loadTabMessages.mockClear()
  mockSessionListStore.setActiveSession.mockClear()
  mockSessionStore.activeSessionId = SESSION_A
}

describe('findPipelineLocation - 管道归属查找', () => {
  beforeEach(resetAll)

  it('内存缓存命中（pipelineSessionMap）→ 直接返回缓存会话', async () => {
    mockPipelineStore.pipelineSessionMap = { 'pipe-1': SESSION_A }
    mockTabStore.pipelineTabMap = { 'pipe-1': 'tab-1' }

    const loc = await findPipelineLocation('pipe-1')
    expect(loc).toEqual({ sessionId: SESSION_A, pipelineId: 'pipe-1', tabId: 'tab-1' })
  })

  it('缓存与会话列表权威不一致 → 以权威为准并修正缓存（registerPipeline）', async () => {
    mockSessions = [
      { id: SESSION_A, pipelineIds: [] },
      { id: SESSION_B, pipelineIds: ['pipe-2'] },
    ]
    mockPipelineStore.pipelineSessionMap = { 'pipe-2': SESSION_A } // 过期缓存
    mockTabStore.tabs = [{ id: 'tab-2', pipelineRunId: 'pipe-2' }]

    const loc = await findPipelineLocation('pipe-2')
    expect(loc).toEqual({ sessionId: SESSION_B, pipelineId: 'pipe-2', tabId: 'tab-2' })
    expect(mockPipelineStore.registerPipeline).toHaveBeenCalledWith(
      expect.objectContaining({ pipelineId: 'pipe-2', sessionId: SESSION_B, level: 2 }),
    )
  })

  it('缓存未命中但会话列表命中 → 第二级直接使用权威会话', async () => {
    mockSessions = [{ id: SESSION_A, pipelineIds: ['pipe-3'] }]
    const loc = await findPipelineLocation('pipe-3')
    expect(loc).toEqual({ sessionId: SESSION_A, pipelineId: 'pipe-3', tabId: null })
    expect(mockPipelineStore.registerPipeline).not.toHaveBeenCalled()
  })

  it('缓存与会话列表都未命中 → forceReloadSessions 强制重拉后命中（tabId=null）', async () => {
    mockSessions = [] // 第一级/第二级都查不到
    fetchedSessions = [{ id: SESSION_B, pipelineIds: ['pipe-far'] }] // 拉取后回填

    const loc = await findPipelineLocation('pipe-far')
    expect(loc).toEqual({ sessionId: SESSION_B, pipelineId: 'pipe-far', tabId: null })
  })

  it('兜底重查后仍无 → 返回 null', async () => {
    mockSessions = []
    fetchedSessions = []
    const loc = await findPipelineLocation('pipe-miss')
    expect(loc).toBeNull()
  })

  it('forceReloadSessions 抛错 → 不向上抛（捕获 console.error）并返回 null', async () => {
    mockSessions = []
    mockEnsureThrows = true
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const loc = await findPipelineLocation('pipe-fail')
    expect(loc).toBeNull()
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('fetchSessions API 调用失败'),
      expect.any(Error),
    )
    errSpy.mockRestore()
  })
})

describe('navigateToPipeline - 全局导航', () => {
  beforeEach(resetAll)

  it('无活跃会话 → 上报通知（当前没有活跃会话，normal 不弹面板）并返回 false', async () => {
    mockSessionStore.activeSessionId = null
    const ok = await navigateToPipeline('pipe-x')
    expect(ok).toBe(false)
    const { notifications, isPanelOpen } = useNotificationStore.getState()
    expect(isPanelOpen).toBe(false)
    expect(
      notifications.some(
        (n) => n.message.includes('无法跳转到管道 pipe-x') && n.message.includes('没有活跃会话'),
      ),
    ).toBe(true)
  })

  it('当前活跃 Tab 的 pipelineRunId 已是目标管道且管道确属当前会话 → 快速返回 true（不切）', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    mockSessions = [{ id: SESSION_A, pipelineIds: ['pipe-me'] }]
    mockTabStore.tabs = [{ id: 'tab-act', pipelineRunId: 'pipe-me' }]
    mockTabStore.activeTabId = 'tab-act'

    const ok = await navigateToPipeline('pipe-me')
    expect(ok).toBe(true)
    expect(mockTabStore.switchToTab).not.toHaveBeenCalled()
  })

  it('残留绑定恰等：活跃 Tab 绑定等于目标管道但管道归属他会话 → 不误判已就位，切到真实归属', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    // 目标管道权威归属 SESSION_B；当前活跃 Tab 持残留绑定 'pipe-b'（恰与目标相等）
    mockPipelineStore.pipelineSessionMap = { 'pipe-b': SESSION_B }
    mockSessions = [
      { id: SESSION_A, pipelineIds: [] },
      { id: SESSION_B, pipelineIds: ['pipe-b'] },
    ]
    mockTabStore.tabs = [{ id: 'tab-a', pipelineRunId: 'pipe-b' }]
    mockTabStore.activeTabId = 'tab-a'

    const ok = await navigateToPipeline('pipe-b')
    // 不得因绑定相等提前返回：归属他会话须继续正常导航（切会话 → 切已有标签）
    expect(ok).toBe(true)
    expect(mockSessionListStore.setActiveSession).toHaveBeenCalledWith(SESSION_B)
    expect(mockTabStore.initSessionTabs).toHaveBeenCalledWith(SESSION_B)
    expect(mockTabStore.switchToTab).toHaveBeenCalledWith('tab-a')
  })

  it('找不到管道归属 → 上报通知（含原因与 pipelineId，高优先级自动弹面板）并返回 false', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    const ok = await navigateToPipeline('pipe-nowhere')
    expect(ok).toBe(false)
    const { notifications, isPanelOpen } = useNotificationStore.getState()
    expect(isPanelOpen).toBe(true)
    expect(
      notifications.some(
        (n) =>
          n.message.includes('无法跳转到管道 pipe-nowhere') &&
          n.message.includes('所有会话中都找不到该管道'),
      ),
    ).toBe(true)
  })

  it('管道在其他会话 → 切换会话（saveCurrentTabs + setActiveSession + initSessionTabs）后切到已有标签', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    mockSessions = [{ id: SESSION_B, pipelineIds: ['pipe-b'] }]
    mockTabStore.tabs = [
      { id: 'tab-a', pipelineRunId: 'pipe-a' },
      { id: 'tab-b', pipelineRunId: 'pipe-b' },
    ]
    mockTabStore.pipelineTabMap = {}

    const ok = await navigateToPipeline('pipe-b')
    expect(ok).toBe(true)
    expect(mockTabStore.saveCurrentTabs).toHaveBeenCalled()
    expect(mockSessionListStore.setActiveSession).toHaveBeenCalledWith(SESSION_B)
    expect(mockTabStore.initSessionTabs).toHaveBeenCalledWith(SESSION_B)
    expect(mockTabStore.switchToTab).toHaveBeenCalledWith('tab-b')
  })

  it('目标会话在列表中已不存在（数据不一致）→ 上报通知并中止返回 false', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    // pipelineSessionMap 指向 SESSION_B，但 readSessions 里没有该会话
    mockPipelineStore.pipelineSessionMap = { 'pipe-ghost': SESSION_B }
    mockSessions = [{ id: SESSION_A, pipelineIds: [] }]
    const ok = await navigateToPipeline('pipe-ghost')
    expect(ok).toBe(false)
    const { notifications } = useNotificationStore.getState()
    expect(
      notifications.some((n) => n.message.includes('pipe-ghost') && n.message.includes('已不存在')),
    ).toBe(true)
    expect(mockTabStore.switchToTab).not.toHaveBeenCalled()
  })

  it('主管道（mainPipelineIdOf 命中）→ 切到 main-{sessionId} 主标签', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    mockSessions = [{ id: SESSION_A, pipelineIds: [MAIN_PIPE_A] }]
    // 主会话无 pipelineSessionMap 记录 → 走权威会话分支
    mockTabStore.tabs = [{ id: `main-${SESSION_A}`, pipelineRunId: MAIN_PIPE_A }]

    const ok = await navigateToPipeline(MAIN_PIPE_A)
    expect(ok).toBe(true)
    expect(mockTabStore.switchToTab).toHaveBeenCalledWith(`main-${SESSION_A}`)
    expect(mockTabStore.openSubAgentTab).not.toHaveBeenCalled()
  })

  it('主管道判定命中但主标签缺失（数据不一致 bug）→ 上报通知并返回 false', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    mockSessions = [{ id: SESSION_A, pipelineIds: [MAIN_PIPE_A] }]
    mockTabStore.tabs = [] // 主标签缺失

    const ok = await navigateToPipeline(MAIN_PIPE_A)
    expect(ok).toBe(false)
    const { notifications } = useNotificationStore.getState()
    expect(
      notifications.some(
        (n) => n.message.includes(MAIN_PIPE_A) && n.message.includes('主标签缺失'),
      ),
    ).toBe(true)
  })

  it('子管道：未注册 → registerPipeline + openSubAgentTab + loadTabMessages，返回 true', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    // 会话有两个管道：权威 activePipelineId 是主管道，pipe-sub-1 是子管道
    mockSessions = [
      { id: SESSION_A, pipelineIds: ['pipe-main', 'pipe-sub-1'], activePipelineId: 'pipe-main' },
    ]
    mockTabStore.tabs = []

    const ok = await navigateToPipeline('pipe-sub-1', {
      agentName: '子Agent',
      agentLevel: 3,
      taskId: 'task-9',
      status: 'running',
    })
    expect(ok).toBe(true)
    expect(mockPipelineStore.registerPipeline).toHaveBeenCalledWith(
      expect.objectContaining({
        pipelineId: 'pipe-sub-1',
        sessionId: SESSION_A,
        level: 3,
        agentName: '子Agent',
      }),
    )
    expect(mockTabStore.openSubAgentTab).toHaveBeenCalledWith(
      expect.objectContaining({
        agentId: 'task-9',
        parentRecordId: 'pipe-sub-1',
        agentLevel: 3,
        pipelineId: 'pipe-sub-1',
        setActive: true,
      }),
    )
    expect(mockTabStore.loadTabMessages).toHaveBeenCalledWith('sub-pipe-sub-1', 'pipe-sub-1')
  })

  it('子管道已注册（pipelines 含该管道）→ 不重复 registerPipeline，仅开标签', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    mockSessions = [
      { id: SESSION_A, pipelineIds: ['pipe-main', 'pipe-sub-2'], activePipelineId: 'pipe-main' },
    ]
    mockPipelineStore.pipelines = { 'pipe-sub-2': { id: 'pipe-sub-2' } }
    mockTabStore.tabs = []

    const ok = await navigateToPipeline('pipe-sub-2')
    expect(ok).toBe(true)
    expect(mockPipelineStore.registerPipeline).not.toHaveBeenCalled()
    expect(mockTabStore.openSubAgentTab).toHaveBeenCalled()
  })

  it('navigateToPipeline 缺省参数：agentName 默认子任务、agentLevel 2、status running', async () => {
    mockSessionStore.activeSessionId = SESSION_A
    mockSessions = [
      { id: SESSION_A, pipelineIds: ['pipe-main', 'pipe-sub-3'], activePipelineId: 'pipe-main' },
    ]
    mockTabStore.tabs = []

    await navigateToPipeline('pipe-sub-3')
    expect(mockPipelineStore.registerPipeline).toHaveBeenCalledWith(
      expect.objectContaining({ level: 2, agentName: '子任务', status: 'running' }),
    )
    expect(mockTabStore.openSubAgentTab).toHaveBeenCalledWith(
      expect.objectContaining({ agentId: 'pipe-sub-3', agentLevel: 2 }),
    )
  })
})
