/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 任务条目「打开对话」的导航失败兜底：归属会话已解析但导航器 reject 时，
 * 记日志并回退到直接切换归属会话（不把用户晾在原地）。
 *
 * mock 边界：会话列表 query（网络）、导航器（跨视图跳转）、会话切换（列表 store）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionStore } from '@/stores/sessionStore'
import { openEntryConversation } from '../PipelineManagerEntryOpen'
import type { PipelineViewEntry } from '@/types/pipeline'

const navMock = vi.hoisted(() => ({ navigateToPipeline: vi.fn() }))
vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: (...args: unknown[]) => navMock.navigateToPipeline(...args),
}))

const sessionsMock = vi.hoisted(() => ({
  readSessions: vi.fn(),
  ensureSessionsLoaded: vi.fn(),
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: () => sessionsMock.readSessions(),
  ensureSessionsLoaded: () => sessionsMock.ensureSessionsLoaded(),
}))

const sessionListMock = vi.hoisted(() => ({
  setActiveSession: vi.fn(),
  rebuildSessionFromThread: vi.fn(),
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: {
    getState: () => ({
      setActiveSession: (...args: unknown[]) => sessionListMock.setActiveSession(...args),
      rebuildSessionFromThread: (...args: unknown[]) =>
        sessionListMock.rebuildSessionFromThread(...args),
    }),
  },
}))

const tabStoreMock = vi.hoisted(() => ({
  tabs: [] as Array<{ id: string; pipelineRunId?: string }>,
  switchToTab: vi.fn(),
  openSubAgentTab: vi.fn(),
  loadTabMessages: vi.fn(),
}))
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: { getState: () => tabStoreMock },
}))

function makeEntry(overrides: Partial<PipelineViewEntry> = {}): PipelineViewEntry {
  return {
    key: 'pipe-1',
    pipelineId: 'pipe-1',
    runId: 'run-1',
    threadId: 'sess-1',
    kind: 'task',
    name: '子任务',
    status: 'running',
    startedAt: '2026-10-28T00:00:00Z',
    ...overrides,
  } as PipelineViewEntry
}

beforeEach(() => {
  vi.clearAllMocks()
  useNotificationStore.setState({ notifications: [] })
  useSessionStore.setState({ activeSessionId: 'sess-cur' } as never)
  sessionsMock.readSessions.mockReturnValue([{ id: 'sess-1', title: '会话一' }])
  sessionsMock.ensureSessionsLoaded.mockResolvedValue(undefined)
  sessionListMock.setActiveSession.mockResolvedValue(undefined)
})

describe('openEntryConversation — 导航失败兜底', () => {
  it('导航器 reject → 回退切换归属会话（不中止在错误态）', async () => {
    navMock.navigateToPipeline.mockRejectedValue(new Error('导航炸了'))
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    await openEntryConversation(makeEntry())

    expect(sessionListMock.setActiveSession).toHaveBeenCalledWith('sess-1')
    consoleError.mockRestore()
  })

  it('导航成功 → 不重复切换会话（正常路径零兜底副作用）', async () => {
    navMock.navigateToPipeline.mockResolvedValue(true)

    await openEntryConversation(makeEntry())

    expect(navMock.navigateToPipeline).toHaveBeenCalledWith(
      'pipe-1',
      expect.objectContaining({ agentName: '子任务' }),
    )
    expect(sessionListMock.setActiveSession).not.toHaveBeenCalled()
  })
})
