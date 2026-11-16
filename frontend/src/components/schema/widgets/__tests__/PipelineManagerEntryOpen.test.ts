// @feature: FP-T12 前端适配（任务条目打开对话统一跳转） | @ci: frontend-test
/**
 * openEntryConversation 统一跳转逻辑测试（自 PipelineManagerWidget 提取的
 * 模块级函数，行为零变化——提取时未随迁用例，本件补齐模块直测）。
 *
 * 覆盖分支：project 早退 / 会话列表拉取失败阻断（通知不误建标签）/
 * threadId 命中主路径 / 血缘 originSessionId 兜底（fallbackSessionId）/
 * 导航拒绝切归属会话 / 切会话失败通知 / 无归属无活跃会话三态
 * （thread 重建成功、重建失败通知、真孤儿显式反馈）/ 有活跃会话时
 * 已有子标签跳转与新建子标签。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PipelineViewEntry } from '@/types/pipeline'

vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: vi.fn(),
  ensureSessionsLoaded: vi.fn(),
}))
vi.mock('@/services/pipelineNavigator', () => ({ navigateToPipeline: vi.fn() }))
vi.mock('@/stores/agentTabStore', () => {
  const useAgentTabStore = Object.assign(vi.fn(), { getState: vi.fn() })
  return { useAgentTabStore }
})
vi.mock('@/stores/notificationStore', () => {
  const useNotificationStore = Object.assign(vi.fn(), { getState: vi.fn() })
  return { useNotificationStore }
})
vi.mock('@/stores/sessionListStore', () => {
  const useSessionListStore = Object.assign(vi.fn(), { getState: vi.fn() })
  return { useSessionListStore }
})
vi.mock('@/stores/sessionStore', () => {
  const useSessionStore = Object.assign(vi.fn(), { getState: vi.fn() })
  return { useSessionStore }
})

import { openEntryConversation } from '@/components/schema/widgets/PipelineManagerEntryOpen'
import { readSessions, ensureSessionsLoaded } from '@/hooks/queries/useSessionsQuery'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'

function makeEntry(overrides: Partial<PipelineViewEntry> = {}): PipelineViewEntry {
  return {
    key: 'pipe-1',
    pipelineId: 'pipe-1',
    runId: 'run-1',
    startedAt: '2026-10-19T00:00:00Z',
    name: '任务A',
    kind: 'task',
    taskId: 'task-1',
    status: 'running',
    ...overrides,
  }
}

function mockStores(opts: {
  sessions?: Array<{ id: string }>
  activeSessionId?: string
  tabs?: Array<{ id: string; pipelineRunId: string }>
} = {}) {
  vi.mocked(readSessions).mockReturnValue(opts.sessions ?? [{ id: 's1' }] as never)
  const addNotification = vi.fn()
  vi.mocked(useNotificationStore.getState).mockReturnValue({ addNotification } as never)
  const setActiveSession = vi.fn().mockResolvedValue(undefined)
  const rebuildSessionFromThread = vi.fn()
  vi.mocked(useSessionListStore.getState).mockReturnValue({
    setActiveSession,
    rebuildSessionFromThread,
  } as never)
  vi.mocked(useSessionStore.getState).mockReturnValue({
    activeSessionId: opts.activeSessionId,
  } as never)
  const switchToTab = vi.fn()
  const openSubAgentTab = vi.fn()
  const loadTabMessages = vi.fn()
  vi.mocked(useAgentTabStore.getState).mockReturnValue({
    tabs: opts.tabs ?? [],
    switchToTab,
    openSubAgentTab,
    loadTabMessages,
  } as never)
  return { addNotification, setActiveSession, rebuildSessionFromThread, switchToTab, openSubAgentTab, loadTabMessages }
}

beforeEach(() => {
  // clearAllMocks 只清调用记录不清实现——reject/resolved 实现逐例重置，
  // 否则失败注入会泄漏到后续用例（单跑绿全跑红的顺序依赖）
  vi.clearAllMocks()
  vi.mocked(readSessions).mockReturnValue([{ id: 's1' }] as never)
  vi.mocked(ensureSessionsLoaded).mockResolvedValue(undefined as never)
  vi.mocked(navigateToPipeline).mockResolvedValue(true as never)
})

describe('openEntryConversation 统一跳转逻辑', () => {
  it('kind=project：无管道无会话，直接早退不触碰任何 store', async () => {
    const stores = mockStores()
    await openEntryConversation(makeEntry({ kind: 'project' }))
    expect(readSessions).not.toHaveBeenCalled()
    expect(stores.addNotification).not.toHaveBeenCalled()
  })

  it('会话列表未加载且拉取失败：通知「无法定位对话」并阻断跳转（不误建标签）', async () => {
    const stores = mockStores({ sessions: [] })
    vi.mocked(readSessions).mockReturnValue([] as never)
    vi.mocked(ensureSessionsLoaded).mockRejectedValue(new Error('net') as never)
    await openEntryConversation(makeEntry())
    expect(stores.addNotification).toHaveBeenCalledWith(expect.objectContaining({ title: '无法定位对话' }))
    expect(navigateToPipeline).not.toHaveBeenCalled()
  })

  it('threadId 命中会话列表：导航定位，无 fallbackSessionId', async () => {
    const stores = mockStores({ sessions: [{ id: 's1' }] })
    await openEntryConversation(makeEntry({ threadId: 's1' }))
    expect(navigateToPipeline).toHaveBeenCalledWith(
      'pipe-1',
      expect.objectContaining({ agentLevel: 2, fallbackSessionId: undefined }),
    )
    expect(stores.setActiveSession).not.toHaveBeenCalled()
  })

  it('血缘兜底：threadId 不在列表而 originSessionId 命中 → fallbackSessionId 指血缘会话', async () => {
    mockStores({ sessions: [{ id: 's2' }] })
    await openEntryConversation(makeEntry({ threadId: 'self-loop', originSessionId: 's2' }))
    expect(navigateToPipeline).toHaveBeenCalledWith(
      'pipe-1',
      expect.objectContaining({ fallbackSessionId: 's2' }),
    )
  })

  it('导航拒绝（ok=false）：切换到归属会话承载打开', async () => {
    vi.mocked(navigateToPipeline).mockResolvedValue(false as never)
    const stores = mockStores({ sessions: [{ id: 's1' }] })
    await openEntryConversation(makeEntry({ threadId: 's1' }))
    expect(stores.setActiveSession).toHaveBeenCalledWith('s1')
    expect(stores.addNotification).not.toHaveBeenCalled()
  })

  it('导航拒绝且切会话也失败：通知「无法打开对话」', async () => {
    vi.mocked(navigateToPipeline).mockResolvedValue(false as never)
    const stores = mockStores({ sessions: [{ id: 's1' }] })
    stores.setActiveSession.mockRejectedValue(new Error('x') as never)
    await openEntryConversation(makeEntry({ threadId: 's1' }))
    expect(stores.addNotification).toHaveBeenCalledWith(expect.objectContaining({ title: '无法打开对话' }))
  })

  it('无归属+无活跃会话+有 thread 线索：从 thread 重建会话并切换打开', async () => {
    const stores = mockStores({ sessions: [], activeSessionId: undefined })
    await openEntryConversation(makeEntry({ threadId: 'th-gone' }))
    expect(stores.rebuildSessionFromThread).toHaveBeenCalledWith('th-gone', expect.objectContaining({ pipelineId: 'pipe-1' }))
    expect(stores.setActiveSession).toHaveBeenCalledWith('th-gone')
  })

  it('thread 重建后切换失败：通知「重建打开失败」', async () => {
    const stores = mockStores({ sessions: [], activeSessionId: undefined })
    stores.setActiveSession.mockRejectedValue(new Error('x') as never)
    await openEntryConversation(makeEntry({ threadId: 'th-gone' }))
    expect(stores.addNotification).toHaveBeenCalledWith(
      expect.objectContaining({ title: '无法打开对话', message: expect.stringContaining('重建打开失败') }),
    )
  })

  it('真孤儿（无任何线索且无活跃会话）：显式反馈不伪装成功', async () => {
    const stores = mockStores({ sessions: [], activeSessionId: undefined })
    await openEntryConversation(makeEntry({ threadId: undefined, originSessionId: undefined }))
    expect(stores.rebuildSessionFromThread).not.toHaveBeenCalled()
    expect(stores.addNotification).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining('无归属会话线索') }),
    )
  })

  it('无归属但有活跃会话：已有同管道子标签 → 直接跳转该标签', async () => {
    const stores = mockStores({ sessions: [], activeSessionId: 'active', tabs: [{ id: 'tab-9', pipelineRunId: 'pipe-1' }] })
    await openEntryConversation(makeEntry())
    expect(stores.switchToTab).toHaveBeenCalledWith('tab-9')
    expect(stores.openSubAgentTab).not.toHaveBeenCalled()
  })

  it('无归属但有活跃会话：无已有标签 → 新建子标签并拉取消息', async () => {
    const stores = mockStores({ sessions: [], activeSessionId: 'active', tabs: [] })
    await openEntryConversation(makeEntry())
    expect(stores.openSubAgentTab).toHaveBeenCalledWith(
      expect.objectContaining({ parentRecordId: 'pipe-1', setActive: true, pipelineId: 'pipe-1' }),
    )
    expect(stores.loadTabMessages).toHaveBeenCalledWith('sub-pipe-1', 'pipe-1')
  })
})
