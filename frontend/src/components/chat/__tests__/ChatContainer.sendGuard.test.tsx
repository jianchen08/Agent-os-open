// @feature FP-T12 前端组件补测
/**
 * 功能测试：ChatContainer 发送受理护栏（B9）
 *
 * pid 解析失败不再静默丢消息——回调受理协议（SendMessageReceipt）：
 * - 主标签 pipelineRunId 空 + 会话无主管道可兜底 → 提示「会话管道未就绪，
 *   请稍候重试」+ 返回未受理（false），不上抛外层；
 * - 主标签 pipelineRunId 空 + 会话有主管道 → 兜底解析后照常受理透传（回归）；
 * - 管道就绪 → 原样透传外层并携带管道 ID（受理结果双向透传）；
 * - 子标签不兜底（既有设计）→ 一次性提示「子任务标签不支持发送」+ 未受理。
 *
 * ChatInput 以 stub 替身参与：捕获回调并回传受理结果（「未受理保留输入」的
 * 真实组件行为由 ChatInput.sendReceipt.test.tsx 覆盖）。
 */
import { render, act } from '@testing-library/react'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { ChatContainer } from '../ChatContainer'
import type { SendMessageParams } from '../types'
import type { AgentTab } from '@/types/task'

const chatInputSpy = vi.hoisted(() => ({
  onSendMessage: null as null | ((params: SendMessageParams) => boolean | void),
}))
vi.mock('../ChatInput', () => ({
  ChatInput: (props: { onSendMessage: (params: SendMessageParams) => boolean | void }) => {
    chatInputSpy.onSendMessage = props.onSendMessage
    return <div data-testid="stub-chat-input" />
  },
}))
vi.mock('../MessageList', () => ({
  MessageList: () => <div data-testid="stub-message-list" />,
}))
vi.mock('../ReferenceSelectionRow', () => ({
  ReferenceSelectionRow: () => <div data-testid="stub-reference-row" />,
}))
vi.mock('../PendingInputQueueBar', () => ({
  PendingInputQueueBar: () => <div data-testid="stub-pending-bar" />,
}))
vi.mock('../VotingPanel', () => ({
  VotingPanel: () => <div data-testid="stub-voting-panel" />,
}))
vi.mock('../AgentTabBar', () => ({
  AgentTabBar: () => <div data-testid="stub-tab-bar" />,
}))

const sessionsRef = vi.hoisted(() => ({
  sessions: [] as Array<{ id: string; activePipelineId?: string | null; pipelineIds?: string[] }>,
  /** forceReloadSessions 调用计数（自愈路径断言） */
  reloadCalls: 0,
  /** 重拉写回模拟：非 null 时 reload 调用即把缓存换成这份会话集 */
  freshSessions: null as Array<{ id: string; activePipelineId?: string | null; pipelineIds?: string[] }> | null,
  /** 置 true 时下一次 reload 拒绝（失败分支断言） */
  rejectNextReload: false,
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: () => sessionsRef.sessions,
  // 与真实现同语义：绕过缓存强制重拉并把结果写回缓存（readSessions 随即可见）
  forceReloadSessions: () => {
    sessionsRef.reloadCalls += 1
    if (sessionsRef.rejectNextReload) {
      sessionsRef.rejectNextReload = false
      return Promise.reject(new Error('重拉失败'))
    }
    if (sessionsRef.freshSessions) {
      sessionsRef.sessions = sessionsRef.freshSessions
    }
    return Promise.resolve(sessionsRef.sessions)
  },
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: () => ({ data: {} }),
}))
// 挂起态：避免挂载期网络调用在测试结束后 resolve 触发 act() 告警
vi.mock('@/services/api/config', () => ({
  getDefaults: () => new Promise(() => {}),
  getLLMConfig: () => new Promise(() => {}),
}))

vi.mock('@/stores/agentTabStore', async () => {
  const { create } = await import('zustand')
  // 测试替身 store，形态即被测组件的选择器子集
  const useAgentTabStore = create<any>(() => ({
    tabs: [],
    activeTabId: null,
    unreadCounts: {},
    switchToTab: vi.fn(),
    closeTab: vi.fn(),
    initSessionTabs: vi.fn(),
  }))
  return { useAgentTabStore }
})

const outerSend = vi.fn<(params: SendMessageParams) => boolean | void>(() => undefined)

function makeMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: 'main-1',
    agentId: '',
    agentName: '',
    agentLevel: 1,
    pipelineRunId: 'pipe-run-1',
    path: [],
    status: 'running',
    hasUnread: false,
    canClose: false,
    ...overrides,
  }
}

function makeSubTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: 'sub-1',
    agentId: '',
    agentName: '子任务',
    agentLevel: 2,
    pipelineRunId: '',
    path: ['主管道', '子任务'],
    status: 'running',
    hasUnread: false,
    canClose: true,
    ...overrides,
  }
}

function notificationCountByTitle(title: string): number {
  return useNotificationStore.getState().notifications.filter((n) => n.title === title).length
}

describe('ChatContainer 发送受理护栏（pid 未就绪不静默丢消息）', () => {
  beforeEach(() => {
    outerSend.mockClear()
    outerSend.mockImplementation(() => undefined)
    chatInputSpy.onSendMessage = null
    sessionsRef.sessions = []
    sessionsRef.reloadCalls = 0
    sessionsRef.freshSessions = null
    sessionsRef.rejectNextReload = false
    useAgentTabStore.setState({ tabs: [], activeTabId: null, unreadCounts: {} })
    useNotificationStore.setState({ notifications: [] })
  })

  it('主标签 pipelineRunId 空 + 会话无主管道可兜底 → 未受理 + 「会话管道未就绪」提示，不上抛外层', () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab({ pipelineRunId: '' })],
      activeTabId: 'main-1',
    })
    sessionsRef.sessions = [{ id: 'sess-1', activePipelineId: null, pipelineIds: [] }]

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    const receipt = chatInputSpy.onSendMessage!({ content: '你好' })
    expect(receipt).toBe(false)
    expect(outerSend).not.toHaveBeenCalled()
    expect(notificationCountByTitle('会话管道未就绪')).toBe(1)
  })

  it('主标签 pipelineRunId 空 + 会话有主管道 → 兜底解析主管道后受理透传（回归）', () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab({ pipelineRunId: '' })],
      activeTabId: 'main-1',
    })
    sessionsRef.sessions = [{ id: 'sess-1', activePipelineId: 'pipe-main', pipelineIds: ['pipe-main'] }]

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    const receipt = chatInputSpy.onSendMessage!({ content: '你好' })
    expect(outerSend).toHaveBeenCalledTimes(1)
    expect(outerSend).toHaveBeenCalledWith({ content: '你好', pipelineId: 'pipe-main' })
    expect(receipt).toBeUndefined()
  })

  it('主标签 pipelineRunId 就绪 → 原样透传外层并携带管道 ID', () => {
    useAgentTabStore.setState({ tabs: [makeMainTab()], activeTabId: 'main-1' })
    sessionsRef.sessions = [{ id: 'sess-1', activePipelineId: 'pipe-main', pipelineIds: ['pipe-main'] }]

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    chatInputSpy.onSendMessage!({ content: '内容' })
    expect(outerSend).toHaveBeenCalledTimes(1)
    expect(outerSend).toHaveBeenCalledWith({ content: '内容', pipelineId: 'pipe-run-1' })
  })

  it('外层未受理（false）→ 未受理结果原样回传（ChatInput 据此保留输入）', () => {
    outerSend.mockImplementation(() => false)
    useAgentTabStore.setState({ tabs: [makeMainTab()], activeTabId: 'main-1' })
    sessionsRef.sessions = [{ id: 'sess-1', activePipelineId: 'pipe-main', pipelineIds: ['pipe-main'] }]

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    expect(chatInputSpy.onSendMessage!({ content: '内容' })).toBe(false)
  })

  it('子标签无管道（不兜底，既有设计）→ 未受理 + 「子任务标签不支持发送」一次性提示', () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab(), makeSubTab()],
      activeTabId: 'sub-1',
    })
    sessionsRef.sessions = [{ id: 'sess-1', activePipelineId: 'pipe-main', pipelineIds: ['pipe-main'] }]

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    expect(chatInputSpy.onSendMessage!({ content: '第一条' })).toBe(false)
    expect(outerSend).not.toHaveBeenCalled()
    expect(notificationCountByTitle('子任务标签不支持发送')).toBe(1)

    // 一次性提示契约：重复发送不重复弹（已完成子标签输入框本已禁用，
    // 此处覆盖 running 子标签连点场景）
    chatInputSpy.onSendMessage!({ content: '第二条' })
    expect(outerSend).not.toHaveBeenCalled()
    expect(notificationCountByTitle('子任务标签不支持发送')).toBe(1)
  })
})

/**
 * BUG-28（全局发送断链）自愈契约：主管道解析失败多为会话缓存陈旧/未就绪
 * （列表拉取失败、创建响应与列表重拉的竞态窗口）——「会话管道未就绪」只允许
 * 出现在真正瞬态，且必须带自愈路径：拒发即触发会话列表强制重拉，重试可受理。
 */
describe('发送自愈：主管道解析失败触发会话列表强制重拉（BUG-28）', () => {
  beforeEach(() => {
    outerSend.mockClear()
    outerSend.mockImplementation(() => undefined)
    chatInputSpy.onSendMessage = null
    sessionsRef.sessions = []
    sessionsRef.reloadCalls = 0
    sessionsRef.freshSessions = null
    sessionsRef.rejectNextReload = false
    useAgentTabStore.setState({ tabs: [], activeTabId: null, unreadCounts: {} })
    useNotificationStore.setState({ notifications: [] })
    // 隔离铠甲：通知 store 的 30s 内容指纹去重表是模块级状态（setState 不清），
    // 前一个 describe 刚入列同指纹的「会话管道未就绪」会吞掉本 describe 的入列——
    // 把时钟推过去重窗，用例与文件内顺序解耦（同前 useRealtimeEventsBranches 做法）。
    vi.useFakeTimers({ now: Date.now() + 31_000 })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('主标签 pid 空 + 缓存无会话 → 未受理且触发一次强制重拉（自愈路径存在）', () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab({ pipelineRunId: '' })],
      activeTabId: 'main-1',
    })
    sessionsRef.sessions = []

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    const receipt = chatInputSpy.onSendMessage!({ content: '第一条' })
    expect(receipt).toBe(false)
    expect(outerSend).not.toHaveBeenCalled()
    expect(notificationCountByTitle('会话管道未就绪')).toBe(1)
    expect(sessionsRef.reloadCalls).toBe(1)
  })

  it('重拉写回缓存后重试 → 兜底解析主管道受理（自愈闭环）', () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab({ pipelineRunId: '' })],
      activeTabId: 'main-1',
    })
    sessionsRef.sessions = []
    sessionsRef.freshSessions = [
      { id: 'sess-1', activePipelineId: 'pipe-main', pipelineIds: ['pipe-main'] },
    ]

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    expect(chatInputSpy.onSendMessage!({ content: '第一条' })).toBe(false)
    expect(outerSend).not.toHaveBeenCalled()

    // 重拉已写回缓存（mock 同步换缓存）；用户重试 → 按权威主管道受理
    const receipt = chatInputSpy.onSendMessage!({ content: '第二条' })
    expect(outerSend).toHaveBeenCalledTimes(1)
    expect(outerSend).toHaveBeenCalledWith({ content: '第二条', pipelineId: 'pipe-main' })
    expect(receipt).toBeUndefined()
  })

  it('重拉失败 → 一次性降级提示显式报错，不静默吞错', async () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab({ pipelineRunId: '' })],
      activeTabId: 'main-1',
    })
    sessionsRef.sessions = []
    sessionsRef.rejectNextReload = true

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)
    expect(chatInputSpy.onSendMessage!({ content: '第一条' })).toBe(false)

    await act(async () => {})
    expect(notificationCountByTitle('会话列表刷新失败')).toBe(1)

    // 再次失败不重复弹（一次性降级提示契约），错误仍留控制台
    sessionsRef.rejectNextReload = true
    expect(chatInputSpy.onSendMessage!({ content: '第二条' })).toBe(false)
    await act(async () => {})
    expect(notificationCountByTitle('会话列表刷新失败')).toBe(1)
  })

  it('子标签拒发不触发重拉（子标签无会话面治愈职责）', () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab(), makeSubTab()],
      activeTabId: 'sub-1',
    })
    sessionsRef.sessions = []

    render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)

    expect(chatInputSpy.onSendMessage!({ content: '第一条' })).toBe(false)
    expect(outerSend).not.toHaveBeenCalled()
    expect(sessionsRef.reloadCalls).toBe(0)
  })
})
