// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * BUG-28 续作：真机 R67 拒发复现（真实模块链，非替身 store）
 *
 * 真机证据（R67）：agent-tabs-<sid> 持久化完整（pipelineRunId/pipelineTabMap），
 * 后端 sessions 全部含有效 active_pipeline_id，但发送仍拒（输入保留/无气泡/无
 * busy），重试仍拒。既有 sendGuard 测试用替身 agentTabStore + 替身 sessions 源，
 * 覆盖不到真实恢复链——本文件用真实 agentTabStore + 真 query 缓存 + 真
 * sessionListStore.setActiveSession + 真 ChatContainer 护栏 + router 同款
 * resolveSendTarget 成员校验，按真机时序复现。
 *
 * 外部依赖边界（网络）mock：getSessions（列表拉取/强制重拉）、
 * pipelineMessageStore（消息拉取）、getDefaults/getLLMConfig（挂起避免网络）。
 */
import { render, act } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
}))
// pipelineMessageStore：网络边界 mock（消息拉取不发请求），形态为可调用 hook
// （ChatContainer 以 usePipelineMessageStore(selector) 订阅）+ getState/setState
vi.mock('@/stores/pipelineMessageStore', async () => {
  const { vi: v } = await import('vitest')
  const state = {
    activatePipeline: v.fn((pipelineId: string | null) => {
      state.activePipelineId = pipelineId
    }),
    registerPipeline: v.fn(),
    loadPipelineMessages: v.fn(() => Promise.resolve({ ok: true as const })),
    getMessages: v.fn(() => [] as unknown[]),
    pipelines: {} as Record<string, unknown>,
    messagesByPipeline: {} as Record<string, unknown[]>,
    streamingState: {} as Record<string, unknown>,
    activePipelineId: null as string | null,
  }
  const hook = Object.assign(
    (selector: (s: typeof state) => unknown) => selector(state),
    { getState: () => state, setState: (partial: Record<string, unknown>) => Object.assign(state, partial) },
  )
  return { usePipelineMessageStore: hook, __pipelineMockState: state }
})
vi.mock('@/services/api/config', () => ({
  getDefaults: () => new Promise(() => {}),
  getLLMConfig: () => new Promise(() => {}),
}))
vi.mock('@/hooks/queries/useAgentsQuery', async () => {
  const { queryKeys } = await import('@/services/query/queryKeys')
  const { queryClient } = await import('@/services/query/queryClient')
  return {
    useAgentsQuery: () => ({ data: queryClient.getQueryData(queryKeys.agents) ?? [] }),
    readAgents: () => queryClient.getQueryData(queryKeys.agents) ?? [],
  }
})

import { getSessions } from '@/services/api/session'
import { ChatContainer } from '../ChatContainer'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { readSessions } from '@/hooks/queries/useSessionsQuery'
import { resolveSendTarget } from '@/utils/mappers'
import { mapThreadToSession } from '@/utils/mappers'
import { uiStorage } from '@/utils/storage'
import type { SendMessageParams } from '../types'

const chatInputSpy = vi.hoisted(() => ({
  onSendMessage: null as null | ((params: SendMessageParams) => boolean | void),
}))
vi.mock('../ChatInput', () => ({
  ChatInput: (props: { onSendMessage: (params: SendMessageParams) => boolean | void }) => {
    chatInputSpy.onSendMessage = props.onSendMessage
    return <div data-testid="stub-chat-input" />
  },
}))
vi.mock('../MessageList', () => ({ MessageList: () => <div data-testid="stub-message-list" /> }))
vi.mock('../ReferenceSelectionRow', () => ({
  ReferenceSelectionRow: () => <div data-testid="stub-reference-row" />,
}))
vi.mock('../PendingInputQueueBar', () => ({
  PendingInputQueueBar: () => <div data-testid="stub-pending-bar" />,
}))
vi.mock('../AgentTabBar', () => ({ AgentTabBar: () => <div data-testid="stub-tab-bar" /> }))

const mockedGetSessions = vi.mocked(getSessions)

/** 真机 R67 localStorage 快照形状（链路通会话 thread-a72ad5cd） */
const SESSION_ID = 'thread-a72ad5cd-1111'
const MAIN_PID = 'f36831f2ee89'
const PERSISTED_TABS = {
  tabs: [
    {
      id: `main-${SESSION_ID}`,
      agentId: 'agentos',
      agentName: '灵汐',
      agentLevel: 1,
      pipelineRunId: MAIN_PID,
      path: ['主管道'],
      status: 'running',
      hasUnread: false,
      canClose: false,
      messages: [],
    },
  ],
  activeTabId: `main-${SESSION_ID}`,
  pipelineTabMap: { [MAIN_PID]: `main-${SESSION_ID}` },
  savedAt: Date.now(),
}

function backendThread(threadId: string, activePipelineId: string) {
  return {
    thread_id: threadId,
    title: '链路通',
    current_state: 'active',
    intent: null,
    created_at: '2026-09-15T10:00:00Z',
    updated_at: '2026-09-16T08:00:00Z',
    agent_id: 'agentos',
    pipeline_ids: [activePipelineId],
    active_pipeline_id: activePipelineId,
    message_count: 2,
    metadata: { session_type: 'main_pipeline' },
  }
}

function seedSessionsCache(threads: ReturnType<typeof backendThread>[]) {
  queryClient.setQueryData(queryKeys.sessions, threads.map(mapThreadToSession))
}

function notificationCountByTitle(title: string): number {
  return useNotificationStore.getState().notifications.filter((n) => n.title === title).length
}

/** router.handleSendMessage 同款成员校验（真实组合，非复制逻辑：直接调 resolveSendTarget） */
function routerGuardAccepts(params: SendMessageParams): boolean {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return false
  const session = readSessions().find((s) => s.id === sid)
  return (
    resolveSendTarget(
      params.pipelineId,
      session,
      useAgentTabStore.getState().pipelineTabMap,
    ) !== undefined
  )
}

describe('BUG-28 续作：真实模块链复现（持久化完整 + 缓存就绪 → 一次点击即受理）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    queryClient.clear()
    useNotificationStore.setState({ notifications: [] })
    useSessionStore.setState({ activeSessionId: null })
    useAgentTabStore.setState({
      tabs: [],
      activeTabId: null,
      unreadCounts: {},
      currentSessionId: null,
      pipelineTabMap: {},
    })
    localStorage.setItem(`agent-tabs-${SESSION_ID}`, JSON.stringify(PERSISTED_TABS))
  })

  it('真机时序：切入会话（setActiveSession→initSessionTabs）后发送一次即受理', async () => {
    // 缓存就绪（后台刷新已完成，后端权威主管道有效）
    seedSessionsCache([backendThread(SESSION_ID, MAIN_PID)])
    mockedGetSessions.mockResolvedValue(
      queryClient.getQueryData(queryKeys.sessions) as never,
    )

    await act(async () => {
      await useSessionListStore.getState().setActiveSession(SESSION_ID)
    })
    useSessionStore.setState({ activeSessionId: SESSION_ID })

    const { tabs, activeTabId } = useAgentTabStore.getState()
    expect(activeTabId).toBe(`main-${SESSION_ID}`)
    const activeTab = tabs.find((t) => t.id === activeTabId)
    expect(activeTab?.pipelineRunId).toBe(MAIN_PID)

    const outer = vi.fn(() => undefined)
    render(
      <QueryClientProvider client={queryClient}>
        <ChatContainer sessionId={SESSION_ID} onSendMessage={outer} />
      </QueryClientProvider>,
    )

    const receipt = chatInputSpy.onSendMessage!({ content: '真机复现' })
    expect(outer).toHaveBeenCalledTimes(1)
    expect(outer).toHaveBeenCalledWith({ content: '真机复现', pipelineId: MAIN_PID })
    expect(receipt).not.toBe(false)
    // router 同款成员校验放行（不落「发送已终止」）
    expect(routerGuardAccepts({ content: '真机复现', pipelineId: MAIN_PID })).toBe(true)
    expect(notificationCountByTitle('会话管道未就绪')).toBe(0)
  })

  it('恢复时序红绿：缓存未就绪时 initSessionTabs 不丢持久化绑定，缓存就绪后一次点击即受理', async () => {
    // 冷启动竞态窗口：setActiveSession 时缓存尚空（query 未返回/挂起）
    mockedGetSessions.mockImplementation(async () => {
      // 模拟首次拉取：回填缓存（相当于 query 成功返回）
      const threads = [backendThread(SESSION_ID, MAIN_PID)]
      return threads.map(mapThreadToSession) as never
    })

    // 缓存为空时切入（setActiveSession 早退——会话不在缓存）：
    await act(async () => {
      await useSessionListStore.getState().setActiveSession(SESSION_ID)
    })
    // 早退：activeSessionId 未设置（真机侧栏此时也不应显示该会话）
    expect(useSessionStore.getState().activeSessionId).toBeNull()

    // query 数据到位 → restoreActiveSessionIfNeeded 真实路径恢复选中
    // （last_active_session 持久化指向本会话，同真机 R67 取证值）
    localStorage.setItem('last_active_session', JSON.stringify(SESSION_ID))
    seedSessionsCache([backendThread(SESSION_ID, MAIN_PID)])
    await act(async () => {
      await useSessionListStore.getState().restoreActiveSessionIfNeeded(
        readSessions(),
      )
    })
    expect(useSessionStore.getState().activeSessionId).toBe(SESSION_ID)

    const activeTab = useAgentTabStore
      .getState()
      .tabs.find((t) => t.id === useAgentTabStore.getState().activeTabId)
    expect(activeTab?.pipelineRunId).toBe(MAIN_PID)

    const outer = vi.fn(() => undefined)
    render(
      <QueryClientProvider client={queryClient}>
        <ChatContainer sessionId={SESSION_ID} onSendMessage={outer} />
      </QueryClientProvider>,
    )
    const receipt = chatInputSpy.onSendMessage!({ content: '恢复后首条' })
    expect(outer).toHaveBeenCalledTimes(1)
    expect(receipt).not.toBe(false)
  })

  it('真机 R67 时序：缓存不可用 + 持久化绑定完整 → 一次点击即受理（不依赖自愈重试）', () => {
    // 会话查询不可用（缓存空）的窗口内，会话面由持久化绑定恢复：
    // 修复前 buildInitialTabs 把持久化 pipelineRunId 清洗为空 → pid 解析恒空拒发
    localStorage.setItem(`agent-tabs-${SESSION_ID}`, JSON.stringify(PERSISTED_TABS))
    useSessionStore.setState({ activeSessionId: SESSION_ID })

    const outer = vi.fn(() => undefined)
    render(
      <QueryClientProvider client={queryClient}>
        <ChatContainer sessionId={SESSION_ID} onSendMessage={outer} />
      </QueryClientProvider>,
    )

    const receipt = chatInputSpy.onSendMessage!({ content: '缓存故障窗口首条' })
    expect(outer).toHaveBeenCalledTimes(1)
    expect(outer).toHaveBeenCalledWith({ content: '缓存故障窗口首条', pipelineId: MAIN_PID })
    expect(receipt).not.toBe(false)
    // router 同款成员校验放行（pipelineTabMap 随持久化 Tab 重建保留成员）
    expect(routerGuardAccepts({ content: '缓存故障窗口首条', pipelineId: MAIN_PID })).toBe(true)
  })
})
