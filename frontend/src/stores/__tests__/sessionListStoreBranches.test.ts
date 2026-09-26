/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * sessionListStore 分支补测：补齐既有测试未触达的分支——
 * restoreActiveSessionIfNeeded（幂等/无记录/失效记录）、deleteSession 成功路径的
 * 全量本地清理（管道数据/标签/localStorage/选中态）、setActiveSession 的
 * agent 匹配与主管道缺失/加载失败分支、各写操作的 API 失败回滚与通知、
 * copySession 的 agentId 透传、autoRenameSessionIfNeeded 的提取兜底链。
 *
 * 网络层（session API / WS）是外部依赖，用 mock 替换；数据面用真实
 * queryClient 缓存与真实 store 断言可观察行为。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { clearSessionExecutionOptions } from '@/services/sessionExecutionOptions'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useAgentStore } from '../agentStore'
import { useNotificationStore } from '../notificationStore'
import { usePipelineMessageStore } from '../pipelineMessageStore'
import { useSessionListStore } from '../sessionListStore'
import { useSessionStore } from '../sessionStore'
import type { Session } from '@/types/models'

vi.mock('../agentTabStore', () => ({
  useAgentTabStore: {
    getState: () => ({
      currentSessionId: agentTab.currentSessionId,
      initSessionTabs: agentTab.initSessionTabs,
      resetAllTabs: agentTab.resetAllTabs,
    }),
  },
}))

const agentTab = {
  currentSessionId: null as string | null,
  initSessionTabs: vi.fn(),
  resetAllTabs: vi.fn(),
}

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  updateSessionAgent: vi.fn(),
  updateSession: vi.fn(),
  mergeConsecutiveAssistantMessages: (m: unknown[]) => m,
}))

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendCancel: vi.fn() },
}))

vi.mock('@/services/sessionExecutionOptions', () => ({
  clearSessionExecutionOptions: vi.fn(),
}))

import * as sessionApi from '@/services/api/session'

const makeSession = (id: string, extra: Partial<Session> = {}): Session =>
  ({
    id,
    title: `会话-${id}`,
    createdAt: '2026-09-01T00:00:00.000Z',
    updatedAt: '2026-09-10T00:00:00.000Z',
    ...extra,
  }) as Session

function seedSessions(sessions: Session[]) {
  queryClient.setQueryData<Session[]>(queryKeys.sessions, sessions)
}

function seedAgents(agents: Array<Record<string, unknown>>) {
  queryClient.setQueryData(queryKeys.agents, agents as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  queryClient.clear()
  useNotificationStore.getState().clearAll()
  useSessionStore.setState({ activeSessionId: null, deletingSessionIds: new Set() })
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    pipelines: {},
    pipelineSessionMap: {},
    streamingState: {},
    topCursorsByPipeline: {},
    bottomCursorsByPipeline: {},
    hasMoreOlderByPipeline: {},
    isLoadingOlderByPipeline: {},
  })
  useAgentStore.setState({ currentAgentId: null })
  agentTab.currentSessionId = null
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('sessionListStore — restoreActiveSessionIfNeeded', () => {
  it('当前活跃会话仍有效时不动作（后台刷新重跑幂等）', async () => {
    seedSessions([makeSession('s1')])
    useSessionStore.setState({ activeSessionId: 's1' })
    const setActiveSpy = vi.spyOn(useSessionListStore.getState(), 'setActiveSession')

    await useSessionListStore.getState().restoreActiveSessionIfNeeded([makeSession('s1')])

    expect(setActiveSpy).not.toHaveBeenCalled()
    setActiveSpy.mockRestore()
  })

  it('当前活跃失效时用持久化的 last_active_session 恢复', async () => {
    seedSessions([makeSession('s1'), makeSession('s2')])
    useSessionStore.setState({ activeSessionId: 'stale' })
    localStorage.setItem('last_active_session', JSON.stringify('s2'))

    await useSessionListStore.getState().restoreActiveSessionIfNeeded([
      makeSession('s1'),
      makeSession('s2'),
    ])

    expect(useSessionStore.getState().activeSessionId).toBe('s2')
  })

  it('无持久化记录时不恢复（保持 null）', async () => {
    seedSessions([makeSession('s1')])
    await useSessionListStore.getState().restoreActiveSessionIfNeeded([makeSession('s1')])
    expect(useSessionStore.getState().activeSessionId).toBeNull()
  })

  it('持久化记录已不在列表中时不恢复（失效 id 被忽略）', async () => {
    seedSessions([makeSession('s1')])
    localStorage.setItem('last_active_session', JSON.stringify('gone'))

    await useSessionListStore.getState().restoreActiveSessionIfNeeded([makeSession('s1')])

    expect(useSessionStore.getState().activeSessionId).toBeNull()
  })

  it('空列表时不恢复（validSessionIds 为空集）', async () => {
    localStorage.setItem('last_active_session', JSON.stringify('s1'))
    await useSessionListStore.getState().restoreActiveSessionIfNeeded([])
    expect(useSessionStore.getState().activeSessionId).toBeNull()
  })
})

describe('sessionListStore — deleteSession 成功路径全量清理', () => {
  function seedPipelinesForSession() {
    usePipelineMessageStore.setState({
      messagesByPipeline: { 'p-main': [], 'p-other': [] } as never,
      pipelines: { 'p-main': { pipelineId: 'p-main' } as never, 'p-other': {} as never },
      pipelineSessionMap: { 'p-main': 's1', 'p-other': 's2' } as never,
      streamingState: { 'p-main': { pipelineId: 'p-main' } as never },
      topCursorsByPipeline: { 'p-main': 5 } as never,
      bottomCursorsByPipeline: { 'p-main': 9 } as never,
      hasMoreOlderByPipeline: { 'p-main': true } as never,
      isLoadingOlderByPipeline: { 'p-main': true } as never,
    })
  }

  it('删除成功后清理该会话的全部管道数据、其余会话数据保留', async () => {
    seedSessions([makeSession('s1'), makeSession('s2')])
    seedPipelinesForSession()
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    const ps = usePipelineMessageStore.getState()
    expect(Object.keys(ps.messagesByPipeline)).toEqual(['p-other'])
    expect(Object.keys(ps.pipelines)).toEqual(['p-other'])
    expect(Object.keys(ps.pipelineSessionMap)).toEqual(['p-other'])
    expect(ps.streamingState['p-main']).toBeUndefined()
    expect(ps.topCursorsByPipeline['p-main']).toBeUndefined()
    expect(ps.bottomCursorsByPipeline['p-main']).toBeUndefined()
    expect(ps.hasMoreOlderByPipeline['p-main']).toBeUndefined()
    expect(ps.isLoadingOlderByPipeline['p-main']).toBeUndefined()
  })

  it('删除成功后从会话缓存移除该会话', async () => {
    seedSessions([makeSession('s1'), makeSession('s2')])
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    const rest = queryClient.getQueryData<Session[]>(queryKeys.sessions)!
    expect(rest.map((s) => s.id)).toEqual(['s2'])
  })

  it('对 pipelineSessionMap 命中的每个管道发取消信号', async () => {
    seedSessions([makeSession('s1')])
    usePipelineMessageStore.setState({
      pipelineSessionMap: { 'p-sub': 's1', 'p-other': 's2' } as never,
    })
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    const targets = vi.mocked(globalWS.sendCancel).mock.calls.map((c) => c[2])
    expect(targets).toContain('p-sub')
    expect(targets).not.toContain('p-other')
  })

  it('会话 id 自身作为主管道时也停止其流式（stopStreaming 覆盖补入的 id）', async () => {
    seedSessions([makeSession('s1')])
    usePipelineMessageStore.setState({
      pipelineSessionMap: { 'p-other': 's2' } as never,
      streamingState: { s1: { pipelineId: 's1' } } as never,
    })
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    // stopStreaming 对补入的 session id 生效
    expect(usePipelineMessageStore.getState().streamingState['s1']).toBeUndefined()
  })

  it('活跃会话被删时清空 activeSessionId 与持久化记录', async () => {
    seedSessions([makeSession('s1')])
    useSessionStore.setState({ activeSessionId: 's1' })
    localStorage.setItem('last_active_session', JSON.stringify('s1'))
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    expect(useSessionStore.getState().activeSessionId).toBeNull()
    expect(localStorage.getItem('last_active_session')).toBeNull()
  })

  it('当前标签页属于被删会话时重置标签并清理 localStorage', async () => {
    seedSessions([makeSession('s1')])
    agentTab.currentSessionId = 's1'
    localStorage.setItem('agent-tabs-s1', '[]')
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    expect(agentTab.resetAllTabs).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem('agent-tabs-s1')).toBeNull()
  })

  it('当前标签页属于其它会话时不重置标签', async () => {
    seedSessions([makeSession('s1')])
    agentTab.currentSessionId = 's2'
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    expect(agentTab.resetAllTabs).not.toHaveBeenCalled()
  })

  it('删除成功后清执行选项快照并从 deletingSessionIds 移除该 id', async () => {
    seedSessions([makeSession('s1')])
    useSessionStore.setState({ deletingSessionIds: new Set(['s1']) })
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    expect(clearSessionExecutionOptions).toHaveBeenCalledWith('s1')
    expect(useSessionStore.getState().deletingSessionIds.has('s1')).toBe(false)
  })

  it('删除非活跃会话时保留 activeSessionId', async () => {
    seedSessions([makeSession('s1'), makeSession('s2')])
    useSessionStore.setState({ activeSessionId: 's2' })
    vi.mocked(sessionApi.deleteSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().deleteSession('s1')

    expect(useSessionStore.getState().activeSessionId).toBe('s2')
  })
})

describe('sessionListStore — setActiveSession 分支', () => {
  it('会话带 agentId 且能在 agent 列表中匹配时设置当前 agent', async () => {
    seedSessions([makeSession('s1', { agentId: 'agent-1' } as never)])
    seedAgents([{ id: 'agent-1', name: 'A' }])

    await useSessionListStore.getState().setActiveSession('s1', false)

    expect(useAgentStore.getState().currentAgentId).toBe('agent-1')
  })

  it('agentId 与 agent 的 configId 匹配同样生效（两种匹配键）', async () => {
    seedSessions([makeSession('s1', { agentId: 'cfg-1' } as never)])
    seedAgents([{ id: 'agent-1', configId: 'cfg-1', name: 'A' }])

    await useSessionListStore.getState().setActiveSession('s1', false)

    expect(useAgentStore.getState().currentAgentId).toBe('agent-1')
  })

  it('agentId 无匹配时不改当前 agent（保持原值）', async () => {
    seedSessions([makeSession('s1', { agentId: 'ghost' } as never)])
    seedAgents([{ id: 'agent-1', name: 'A' }])
    useAgentStore.setState({ currentAgentId: 'keep-me' })

    await useSessionListStore.getState().setActiveSession('s1', false)

    expect(useAgentStore.getState().currentAgentId).toBe('keep-me')
  })

  it('会话无 agentId 时不触碰 agent 列表', async () => {
    seedSessions([makeSession('s1')])
    useAgentStore.setState({ currentAgentId: 'keep-me' })

    await useSessionListStore.getState().setActiveSession('s1', false)

    expect(useAgentStore.getState().currentAgentId).toBe('keep-me')
  })

  it('fetchData=false 时不加载消息（仅切换选中态）', async () => {
    seedSessions([makeSession('s1', { pipelineIds: ['p1'] } as never)])
    const loadSpy = vi.spyOn(usePipelineMessageStore.getState(), 'loadPipelineMessages')

    await useSessionListStore.getState().setActiveSession('s1', false)

    expect(loadSpy).not.toHaveBeenCalled()
    expect(useSessionStore.getState().activeSessionId).toBe('s1')
    loadSpy.mockRestore()
  })

  it('fetchData=true 时按主管道加载消息（映射真值 pipelineIds[0]）', async () => {
    seedSessions([makeSession('s1', { pipelineIds: ['p-main'] } as never)])
    const loadSpy = vi
      .spyOn(usePipelineMessageStore.getState(), 'loadPipelineMessages')
      .mockResolvedValue({ ok: true })

    await useSessionListStore.getState().setActiveSession('s1', true)

    expect(loadSpy).toHaveBeenCalledWith('p-main', { threadId: 's1' })
    loadSpy.mockRestore()
  })

  it('无主管道时打 error 日志且不加载', async () => {
    // 映射为空 → mainPipelineIdOf 返回 undefined
    seedSessions([makeSession('s1', { pipelineIds: [] } as never)])
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const loadSpy = vi.spyOn(usePipelineMessageStore.getState(), 'loadPipelineMessages')

    await useSessionListStore.getState().setActiveSession('s1', true)

    expect(loadSpy).not.toHaveBeenCalled()
    expect(errSpy).toHaveBeenCalled()
    errSpy.mockRestore()
    loadSpy.mockRestore()
  })

  it('加载消息抛错时被捕获并打 error（不冒泡）', async () => {
    seedSessions([makeSession('s1', { pipelineIds: ['p-main'] } as never)])
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const loadSpy = vi
      .spyOn(usePipelineMessageStore.getState(), 'loadPipelineMessages')
      .mockRejectedValue(new Error('load failed'))

    await expect(
      useSessionListStore.getState().setActiveSession('s1', true),
    ).resolves.toBeUndefined()

    expect(errSpy).toHaveBeenCalled()
    errSpy.mockRestore()
    loadSpy.mockRestore()
  })

  it('持久化选中会话 id（刷新后可恢复）', async () => {
    seedSessions([makeSession('s1')])
    await useSessionListStore.getState().setActiveSession('s1', false)
    expect(localStorage.getItem('last_active_session')).toBe(JSON.stringify('s1'))
  })

  it('空白 id（纯空格）不执行操作', async () => {
    seedSessions([makeSession('s1')])
    await useSessionListStore.getState().setActiveSession('   ', false)
    expect(useSessionStore.getState().activeSessionId).toBeNull()
  })
})

describe('sessionListStore — 写操作失败回滚与通知', () => {
  it('toggleSessionPin 同步失败时回滚 pinned 并发通知', async () => {
    seedSessions([makeSession('s1', { pinned: false } as never)])
    vi.mocked(sessionApi.updateSession).mockRejectedValue(new Error('network'))

    useSessionListStore.getState().toggleSessionPin('s1')
    expect(
      queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].pinned,
    ).toBe(true)

    // 等 catch 回调
    await vi.waitFor(() => {
      expect(
        queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].pinned,
      ).toBe(false)
    })
    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.message === '置顶状态同步失败，已恢复原状态')
    expect(notif).toBeDefined()
    expect(notif?.category).toBe('error')
  })

  it('renameSession 同步失败时回滚标题与 updatedAt 并发通知', async () => {
    seedSessions([makeSession('s1', { title: '原标题', updatedAt: '2026-01-01T00:00:00.000Z' })])
    vi.mocked(sessionApi.updateSession).mockRejectedValue(new Error('network'))

    await useSessionListStore.getState().renameSession('s1', '新标题')

    const restored = queryClient.getQueryData<Session[]>(queryKeys.sessions)![0]
    expect(restored.title).toBe('原标题')
    expect(restored.updatedAt).toBe('2026-01-01T00:00:00.000Z')
    expect(
      useNotificationStore.getState().notifications.some((n) => n.message === '重命名同步失败，已恢复原标题'),
    ).toBe(true)
  })

  it('renameSession 成功后标题为 trim 后的值', async () => {
    seedSessions([makeSession('s1', { title: '原标题' })])
    vi.mocked(sessionApi.updateSession).mockResolvedValue(undefined as never)

    await useSessionListStore.getState().renameSession('s1', '  新标题  ')

    expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe('新标题')
    expect(sessionApi.updateSession).toHaveBeenCalledWith('s1', { title: '新标题' })
  })

  it('renameSession 对不存在的会话仍调用 API（无本地快照用于回滚）', async () => {
    seedSessions([])
    vi.mocked(sessionApi.updateSession).mockRejectedValue(new Error('404'))

    await useSessionListStore.getState().renameSession('ghost', '标题')

    // 回滚分支用 s.title 兜底（无 prevTitle）
    expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)).toEqual([])
  })
})

describe('sessionListStore — copySession 与 updateSessionAgent', () => {
  it('copySession 标题加「(副本)」并透传原 agentId', async () => {
    seedSessions([makeSession('s1', { title: '原标题', agentId: 'agent-9' } as never)])
    vi.mocked(sessionApi.createSession).mockResolvedValue(
      makeSession('s-copy', { title: '原标题 (副本)' }) as never,
    )

    const created = await useSessionListStore.getState().copySession('s1')

    expect(created.id).toBe('s-copy')
    expect(vi.mocked(sessionApi.createSession).mock.calls[0][0]).toMatchObject({
      title: '原标题 (副本)',
      agentId: 'agent-9',
    })
  })

  it('copySession 原会话无 agentId 时传 undefined（不伪造空串）', async () => {
    seedSessions([makeSession('s1', { title: 'T' })])
    vi.mocked(sessionApi.createSession).mockResolvedValue(makeSession('s-copy') as never)

    await useSessionListStore.getState().copySession('s1')

    expect(vi.mocked(sessionApi.createSession).mock.calls[0][0].agentId).toBeUndefined()
  })

  it('copySession 对不存在的会话抛「会话不存在」', async () => {
    seedSessions([])
    await expect(useSessionListStore.getState().copySession('ghost')).rejects.toThrow('会话不存在')
  })

  it('updateSessionAgent 用 API 返回值刷新缓存', async () => {
    seedSessions([makeSession('s1', { agentId: null } as never)])
    vi.mocked(sessionApi.updateSessionAgent).mockResolvedValue(
      makeSession('s1', { agentId: 'agent-new' } as never) as never,
    )

    await useSessionListStore.getState().updateSessionAgent('s1', 'agent-new')

    expect(
      queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].agentId,
    ).toBe('agent-new')
    expect(sessionApi.updateSessionAgent).toHaveBeenCalledWith('s1', 'agent-new')
  })

  it('updateSessionAgent 传 null（解绑）也生效', async () => {
    seedSessions([makeSession('s1', { agentId: 'agent-1' } as never)])
    vi.mocked(sessionApi.updateSessionAgent).mockResolvedValue(
      makeSession('s1', { agentId: null } as never) as never,
    )

    await useSessionListStore.getState().updateSessionAgent('s1', null)

    expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].agentId).toBeNull()
  })

  it('updateSessionAgent 失败时错误向上传播', async () => {
    seedSessions([makeSession('s1')])
    vi.mocked(sessionApi.updateSessionAgent).mockRejectedValue(new Error('403'))

    await expect(
      useSessionListStore.getState().updateSessionAgent('s1', 'a'),
    ).rejects.toThrow('403')
  })
})

describe('sessionListStore — autoRenameSessionIfNeeded 提取兜底链', () => {
  function seedMessages(pipelineId: string, messages: unknown[]) {
    usePipelineMessageStore.setState({
      messagesByPipeline: { [pipelineId]: messages } as never,
    })
  }

  it('parts 中无 text 类型时回退 content 字段', async () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [
      {
        id: 'm1',
        role: 'user',
        content: '来自 content 的文本',
        parts: [{ type: 'tool-call', content: 'x' }],
      },
    ])
    vi.mocked(sessionApi.updateSession).mockResolvedValue(undefined as never)

    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')

    await vi.waitFor(() => {
      expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe(
        '来自 content 的文本',
      )
    })
  })

  it('parts 的 text 内容为空串时也回退 content', async () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [
      {
        id: 'm1',
        role: 'user',
        content: '兜底文案',
        parts: [{ type: 'text', content: '' }],
      },
    ])
    vi.mocked(sessionApi.updateSession).mockResolvedValue(undefined as never)

    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')

    await vi.waitFor(() => {
      expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe('兜底文案')
    })
  })

  it('无消息时不重命名', () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [])
    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')
    expect(sessionApi.updateSession).not.toHaveBeenCalled()
  })

  it('消息中没有 user 角色时不重命名', () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [{ id: 'm1', role: 'assistant', content: 'hi' }])
    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')
    expect(sessionApi.updateSession).not.toHaveBeenCalled()
  })

  it('user 消息内容全为空白时不重命名', () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [{ id: 'm1', role: 'user', content: '   \n  ' }])
    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')
    expect(sessionApi.updateSession).not.toHaveBeenCalled()
  })

  it('多行文本的换行被替换为空格（标题单行化）', async () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [{ id: 'm1', role: 'user', content: '第一行\n第二行' }])
    vi.mocked(sessionApi.updateSession).mockResolvedValue(undefined as never)

    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')

    await vi.waitFor(() => {
      expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe('第一行 第二行')
    })
  })

  it('恰好 30 字符不截断（边界：仅超过才截断）', async () => {
    const exactly30 = 'a'.repeat(30)
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [{ id: 'm1', role: 'user', content: exactly30 }])
    vi.mocked(sessionApi.updateSession).mockResolvedValue(undefined as never)

    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')

    await vi.waitFor(() => {
      expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe(exactly30)
    })
  })

  it('多个 text part 拼接后再截断', async () => {
    seedSessions([makeSession('s1', { title: '灵汐' })])
    seedMessages('p1', [
      {
        id: 'm1',
        role: 'user',
        parts: [
          { type: 'text', content: 'AAAA' },
          { type: 'text', content: 'BBBB' },
        ],
      },
    ])
    vi.mocked(sessionApi.updateSession).mockResolvedValue(undefined as never)

    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')

    await vi.waitFor(() => {
      expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe('AAAABBBB')
    })
  })

  it('已重命名过的会话（标题非默认值）不再触发', () => {
    seedSessions([makeSession('s1', { title: '用户改过的标题' })])
    seedMessages('p1', [{ id: 'm1', role: 'user', content: '新内容' }])
    useSessionListStore.getState().autoRenameSessionIfNeeded('s1', 'p1')
    expect(sessionApi.updateSession).not.toHaveBeenCalled()
  })
})

describe('sessionListStore — updateSession 未匹配 id', () => {
  it('更新不存在的会话 id 不改动任何条目', () => {
    seedSessions([makeSession('s1', { title: 'A' })])
    useSessionListStore.getState().updateSession('ghost', { title: 'B' })
    expect(queryClient.getQueryData<Session[]>(queryKeys.sessions)![0].title).toBe('A')
  })
})
