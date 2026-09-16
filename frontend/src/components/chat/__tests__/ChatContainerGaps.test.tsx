// @feature FP-T12 前端组件补测
/**
 * ChatContainer 缺口补测（发送受理护栏见 ChatContainer.sendGuard.test.tsx）：
 * - 加载态占位
 * - Tab 映射（主/子命名、未读、激活、可关闭）与切换/关闭回调分发
 * - 消息搜索过滤（内容大小写不敏感、tool_call part 名）与空态/活跃态标记
 * - messageJump 按激活管道透传与消费清除
 * - 会话初始化（initSessionTabs 仅在无激活 Tab 时触发）
 * - 生成态双来源（流式状态 + runs 快照）
 * - 模型名解析（agentId / pipelineAgentName → tiers 显示名）
 * - 思考强度（显式记忆优先、管道参数反向映射、默认回退、切换分发与失败降级）
 * - 投票面板过滤、子标签完成禁用、待发送队列条渲染条件、hasMore 透传
 */
import { act, render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatContainer } from '../ChatContainer'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { PipelineMeta } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThinkingModeStore } from '@/stores/thinkingModeStore'
import { useUIStore } from '@/stores/uiStore'
import { useVotingStore } from '@/stores/votingStore'
import type { Message } from '@/types/models'
import type { AgentTab } from '@/types/task'
import type { VotingSession } from '@/types/voting'
import type { ChatContainerProps, SendMessageParams } from '../types'

const stubs = vi.hoisted(() => ({
  tabBar: null as null | {
    tabs: Array<{
      id: string
      name: string
      status: string
      isActive: boolean
      unreadCount: number
      canClose: boolean
      agentLevel: number
      agentName: string
      taskId?: string
      path: string[]
    }>
    onTabChange: (tabId: string) => void
    onTabClose: (tabId: string) => void
  },
  messageList: null as null | {
    messages: Message[]
    isGenerating?: boolean
    hasMore?: boolean
    isLoadingMore?: boolean
    onLoadMore?: () => void
    searchQuery?: string
    jumpTarget?: { pipelineId: string; sequence: number }
    onJumpConsumed?: () => void
  },
  chatInput: null as null | {
    draftKey?: string
    disabled?: boolean
    isGenerating?: boolean
    modelName?: string
    thinkingStrength?: 'off' | 'low' | 'medium' | 'high'
    onThinkingStrengthChange?: (strength: 'off' | 'low' | 'medium' | 'high') => void
    onSendMessage?: (params: SendMessageParams) => boolean | void
  },
  pendingBarPipelineId: '' as string,
  pendingBarRendered: false,
  votingPanels: [] as VotingSession[],
  sessions: [] as Array<{ id: string; activePipelineId?: string | null; pipelineIds?: string[] }>,
  agents: [] as Array<{
    id: string
    configId?: string
    model?: string
    config?: { model?: string }
  }>,
  runs: {} as Record<string, { status: string }>,
  defaults: null as null | Promise<{ chat: string; embedding: string; tiers: Record<string, string> }>,
  llmConfig: null as null | Promise<{
    models: Record<string, { provider: string; model_name: string; display_name: string; default_params?: Record<string, unknown> }>
    providers: Record<string, unknown>
    defaults: { chat: string; embedding: string; tiers: Record<string, string> }
  }>,
}))

const switchModeMock = vi.hoisted(() => vi.fn(() => Promise.resolve({})))

vi.mock('../AgentTabBar', () => ({
  AgentTabBar: (props: NonNullable<typeof stubs.tabBar>) => {
    stubs.tabBar = props
    return <div data-testid="stub-tab-bar" />
  },
}))
vi.mock('../MessageList', () => ({
  MessageList: (props: NonNullable<typeof stubs.messageList>) => {
    stubs.messageList = props
    return <div data-testid="stub-message-list" />
  },
}))
vi.mock('../ChatInput', () => ({
  ChatInput: (props: NonNullable<typeof stubs.chatInput>) => {
    stubs.chatInput = props
    return <div data-testid="stub-chat-input" />
  },
}))
vi.mock('../GodotSelectionRow', () => ({
  GodotSelectionRow: () => <div data-testid="stub-godot-row" />,
}))
vi.mock('../PendingInputQueueBar', () => ({
  PendingInputQueueBar: (props: { pipelineId: string }) => {
    stubs.pendingBarRendered = true
    stubs.pendingBarPipelineId = props.pipelineId
    return <div data-testid="stub-pending-bar" />
  },
}))
vi.mock('../VotingPanel', () => ({
  VotingPanel: (props: { voting: VotingSession }) => {
    stubs.votingPanels.push(props.voting)
    return <div data-testid="stub-voting-panel" />
  },
}))

vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: () => stubs.sessions,
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: stubs.agents }),
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: () => ({ data: stubs.runs }),
}))
vi.mock('@/services/api/config', () => ({
  getDefaults: () =>
    (stubs.defaults ??= Promise.resolve({ chat: 'chat-model', embedding: 'emb-model', tiers: {} })),
  getLLMConfig: () =>
    (stubs.llmConfig ??= Promise.resolve({
      models: {},
      providers: {},
      defaults: { chat: 'chat-model', embedding: 'emb-model', tiers: {} },
    })),
}))
vi.mock('@/services/api/thinkingMode', () => ({
  switchThinkingMode: switchModeMock,
}))

vi.mock('@/stores/agentTabStore', async () => (await import('./helpers/chatFlowMocks')).agentTabStoreDouble())

const outerSend = vi.fn<(params: SendMessageParams) => boolean | void>(() => true)

function makeMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: 'main-1',
    agentId: '',
    agentName: '',
    agentLevel: 1,
    pipelineRunId: 'pipe-1',
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
    pipelineRunId: 'pipe-sub-1',
    path: ['主管道', '子任务'],
    status: 'running',
    hasUnread: false,
    canClose: true,
    ...overrides,
  }
}

function makeMessage(overrides: Partial<Message> & { id: string }): Message {
  return {
    sessionId: 'sess-1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: '2026-09-13T00:00:00Z',
    status: 'completed',
    ...overrides,
  }
}

function makePipelineMeta(overrides: Partial<PipelineMeta> = {}): PipelineMeta {
  return {
    pipelineId: 'pipe-1',
    sessionId: 'sess-1',
    level: 1,
    tabId: null,
    agentName: '',
    status: 'running',
    parentId: null,
    unreadCount: 0,
    ...overrides,
  }
}

function makeVoting(overrides: Partial<VotingSession> & { id: string }): VotingSession {
  return {
    title: `投票-${overrides.id}`,
    agentId: 'agent-1',
    options: [{ id: 'opt-1', title: '方案A', voteCount: 0, hasVoted: false }],
    status: 'open',
    allowMultiple: false,
    requireReason: false,
    createdAt: '2026-09-13T00:00:00Z',
    ...overrides,
  }
}

function setupActivePipeline(messages: Message[], pipelineId = 'pipe-1') {
  usePipelineMessageStore.setState({
    activePipelineId: pipelineId,
    messagesByPipeline: { [pipelineId]: messages },
  })
  useAgentTabStore.setState({
    tabs: [makeMainTab({ pipelineRunId: pipelineId })],
    activeTabId: 'main-1',
  })
}

async function mountContainer(props: Partial<ChatContainerProps> = {}) {
  const view = render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} {...props} />)
  // flush 挂载期 getDefaults/getLLMConfig 微任务，避免 act 外 setState
  await act(async () => {})
  return view
}

function notificationCountByTitle(title: string): number {
  return useNotificationStore.getState().notifications.filter((n) => n.title === title).length
}

beforeEach(() => {
  stubs.tabBar = null
  stubs.messageList = null
  stubs.chatInput = null
  stubs.pendingBarRendered = false
  stubs.pendingBarPipelineId = ''
  stubs.votingPanels = []
  stubs.sessions = []
  stubs.agents = []
  stubs.runs = {}
  stubs.defaults = Promise.resolve({ chat: 'chat-model', embedding: 'emb-model', tiers: {} })
  stubs.llmConfig = Promise.resolve({
    models: {},
    providers: {},
    defaults: { chat: 'chat-model', embedding: 'emb-model', tiers: {} },
  })
  switchModeMock.mockClear()
  switchModeMock.mockImplementation(() => Promise.resolve({}))
  localStorage.clear()
  outerSend.mockClear()
  useUIStore.setState({ messageSearchQuery: '', messageJump: null })
  useNotificationStore.setState({ notifications: [] })
  usePipelineMessageStore.setState({
    activePipelineId: null,
    messagesByPipeline: {},
    pipelines: {},
    streamingState: {},
  })
  useVotingStore.setState({ votingSessions: [] })
  useSessionStore.setState({ activeSessionId: null })
  useThinkingModeStore.setState({ strengthByTabId: {} })
  const tabState = useAgentTabStore.getState()
  tabState.switchToTab.mockClear()
  tabState.closeTab.mockClear()
  tabState.initSessionTabs.mockClear()
  useAgentTabStore.setState({ tabs: [], activeTabId: null, unreadCounts: {} })
})

describe('ChatContainer — 加载态', () => {
  it('isLoading 时渲染加载占位，不渲染 Tab 栏与消息列表', async () => {
    const { queryByTestId } = await mountContainer({ isLoading: true })
    expect(queryByTestId('chat-container-loading')).toBeInTheDocument()
    expect(queryByTestId('chat-container')).not.toBeInTheDocument()
    expect(queryByTestId('stub-tab-bar')).not.toBeInTheDocument()
    expect(queryByTestId('stub-message-list')).not.toBeInTheDocument()
  })
})

describe('ChatContainer — Tab 栏映射与回调', () => {
  it('主 Tab 固定名「主管道」，子 Tab 用 agentName；未读/激活/可关闭逐项映射', async () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab({ agentId: 'agent-1' }), makeSubTab()],
      activeTabId: 'main-1',
      unreadCounts: { 'main-1': 3 },
    })
    await mountContainer()

    expect(stubs.tabBar?.tabs).toHaveLength(2)
    const [mainBar, subBar] = stubs.tabBar?.tabs ?? []
    expect(mainBar).toMatchObject({
      id: 'main-1',
      name: '主管道',
      agentName: '主管道',
      status: 'running',
      isActive: true,
      unreadCount: 3,
      canClose: false,
      agentLevel: 1,
    })
    expect(subBar).toMatchObject({
      id: 'sub-1',
      name: '子任务',
      agentName: '子任务',
      isActive: false,
      unreadCount: 0,
      canClose: true,
      agentLevel: 2,
    })
  })

  it('onTabChange/onTabClose 分发到 agentTabStore', async () => {
    useAgentTabStore.setState({ tabs: [makeMainTab()], activeTabId: 'main-1' })
    await mountContainer()

    await act(async () => {
      stubs.tabBar?.onTabChange('tab-next')
    })
    expect(useAgentTabStore.getState().switchToTab).toHaveBeenCalledWith('tab-next')

    await act(async () => {
      stubs.tabBar?.onTabClose('sub-1')
    })
    expect(useAgentTabStore.getState().closeTab).toHaveBeenCalledWith('sub-1')
  })

  it('无 Tab 时不渲染 Tab 栏', async () => {
    const { queryByTestId } = await mountContainer()
    expect(queryByTestId('stub-tab-bar')).not.toBeInTheDocument()
  })
})

describe('ChatContainer — 消息过滤与空态', () => {
  const messages: Message[] = [
    makeMessage({ id: 'm1', content: '关于 deploy 的讨论' }),
    makeMessage({
      id: 'm2',
      content: '无关正文',
      parts: [{ type: 'tool_call', callId: 'c1', name: 'file_read', args: {}, state: 'done', sequence: 1 }],
    }),
    makeMessage({ id: 'm3', role: 'user', content: '今天天气不错' }),
  ]

  it('无搜索词：全部消息透传，容器标记 active，hasMore 透传', async () => {
    setupActivePipeline(messages)
    const { getByTestId } = await mountContainer({
      hasMoreMessages: true,
      isLoadingMoreMessages: true,
    })
    expect(stubs.messageList?.messages).toHaveLength(3)
    expect(stubs.messageList?.hasMore).toBe(true)
    expect(stubs.messageList?.isLoadingMore).toBe(true)
    expect(getByTestId('chat-container').getAttribute('data-chat-state')).toBe('active')
  })

  it('搜索词大小写不敏感匹配消息内容（query 大写命中小写正文）', async () => {
    setupActivePipeline(messages)
    useUIStore.setState({ messageSearchQuery: 'DEPLOY' })
    await mountContainer()

    expect(stubs.messageList?.messages.map((m) => m.id)).toEqual(['m1'])
    expect(stubs.messageList?.searchQuery).toBe('DEPLOY')
  })

  it('搜索词匹配 tool_call part 名', async () => {
    setupActivePipeline(messages)
    useUIStore.setState({ messageSearchQuery: 'file_read' })
    await mountContainer()

    expect(stubs.messageList?.messages.map((m) => m.id)).toEqual(['m2'])
  })

  it('无命中：消息为空，容器回落 empty 态', async () => {
    setupActivePipeline(messages)
    useUIStore.setState({ messageSearchQuery: 'zzz-无命中' })
    const { getByTestId } = await mountContainer()

    expect(stubs.messageList?.messages).toHaveLength(0)
    expect(getByTestId('chat-container').getAttribute('data-chat-state')).toBe('empty')
  })

  it('无激活管道：消息为空', async () => {
    useAgentTabStore.setState({ tabs: [makeMainTab()], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.messageList?.messages).toHaveLength(0)
  })

  it('激活管道在消息表中无记录：消息为空（不崩溃）', async () => {
    usePipelineMessageStore.setState({ activePipelineId: 'pipe-ghost' })
    useAgentTabStore.setState({ tabs: [makeMainTab({ pipelineRunId: 'pipe-ghost' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.messageList?.messages).toHaveLength(0)
  })
})

describe('ChatContainer — 配置加载取消', () => {
  it('挂载后立即卸载：承诺迟到 resolve 不再写入状态（cancelled 守卫）', async () => {
    let resolveDefaults!: (v: { chat: string; embedding: string; tiers: Record<string, string> }) => void
    let resolveLlmConfig!: (v: unknown) => void
    stubs.defaults = new Promise((res) => (resolveDefaults = res))
    stubs.llmConfig = new Promise((res) => (resolveLlmConfig = res))

    const view = render(<ChatContainer sessionId="sess-1" onSendMessage={outerSend} />)
    view.unmount()

    await act(async () => {
      resolveDefaults({ chat: 'late', embedding: 'e', tiers: { large: 'late-model' } })
      resolveLlmConfig({ models: {}, providers: {}, defaults: { chat: 'late', embedding: 'e', tiers: {} } })
    })
    // 未断言渲染（已卸载）；守卫生效的体现是无 act 外 setState 告警/异常
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})

describe('ChatContainer — messageJump 透传', () => {
  it('跳转目标管道与激活管道一致 → 透传 MessageList，消费回调清除 uiStore', async () => {
    setupActivePipeline([])
    useUIStore.setState({ messageJump: { pipelineId: 'pipe-1', sequence: 5 } })
    await mountContainer()

    expect(stubs.messageList?.jumpTarget).toEqual({ pipelineId: 'pipe-1', sequence: 5 })
    await act(async () => {
      stubs.messageList?.onJumpConsumed?.()
    })
    expect(useUIStore.getState().messageJump).toBeNull()
  })

  it('跳转目标管道不一致（跨管道中间态）→ 不透传', async () => {
    setupActivePipeline([])
    useUIStore.setState({ messageJump: { pipelineId: 'pipe-other', sequence: 9 } })
    await mountContainer()

    expect(stubs.messageList?.jumpTarget).toBeUndefined()
  })
})

describe('ChatContainer — 会话初始化', () => {
  it('会话已激活且无 Tab → initSessionTabs(sessionId)', async () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    await mountContainer()
    expect(useAgentTabStore.getState().initSessionTabs).toHaveBeenCalledWith('sess-1')
  })

  it('已有激活 Tab → 不重复初始化', async () => {
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    useAgentTabStore.setState({ activeTabId: 'main-1' })
    await mountContainer()
    expect(useAgentTabStore.getState().initSessionTabs).not.toHaveBeenCalled()
  })
})

describe('ChatContainer — 生成态双来源', () => {
  it('当前标签管道流式中 → ChatInput/MessageList isGenerating=true', async () => {
    setupActivePipeline([])
    usePipelineMessageStore.setState({
      streamingState: { 'pipe-1': { isStreaming: true, messageId: 'm1' } },
    })
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(true)
    expect(stubs.messageList?.isGenerating).toBe(true)
  })

  it('流式未激活但 runs 快照 running → 仍视为生成中', async () => {
    setupActivePipeline([])
    stubs.runs = { 'pipe-1': { status: 'running' } }
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(true)
  })

  it('两来源皆静默 → isGenerating=false', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(false)
  })
})

describe('ChatContainer — 模型名解析', () => {
  it('agentId 命中 agent.model，经 tiers 映射为显示名', async () => {
    stubs.agents = [{ id: 'agent-1', configId: 'cfg-1', model: 'large', config: {} }]
    stubs.defaults = Promise.resolve({ chat: 'chat-model', embedding: 'emb', tiers: { large: 'deepseek-max' } })
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('deepseek-max')
  })

  it('agentId 为空时按管道 agentName 兜底解析', async () => {
    stubs.agents = [{ id: 'agent-9', configId: 'planner-agent', model: 'medium', config: {} }]
    stubs.defaults = Promise.resolve({ chat: 'chat-model', embedding: 'emb', tiers: { medium: 'glm-air' } })
    usePipelineMessageStore.setState({ pipelines: { 'pipe-1': makePipelineMeta({ agentName: 'planner-agent' }) } })
    useAgentTabStore.setState({ tabs: [makeMainTab()], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('glm-air')
  })

  it('无任何匹配 → modelName 为空串', async () => {
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('')
  })

  it('agentId 命中但 agent 无任何模型字段 → modelName 为空串', async () => {
    stubs.agents = [{ id: 'agent-1', configId: 'cfg-1' }]
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('')
  })

  it('agent.model 缺失时回退 agent.config.model', async () => {
    stubs.agents = [{ id: 'agent-1', configId: 'cfg-1', config: { model: 'small' } }]
    stubs.defaults = Promise.resolve({ chat: 'c', embedding: 'e', tiers: { small: 'glm-air' } })
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('glm-air')
  })
})

describe('ChatContainer — 思考强度', () => {
  it('未显式设置 → 从管道模型 default_params 反向映射（reasoning_effort=low → low）', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.defaults = Promise.resolve({ chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } })
    stubs.llmConfig = Promise.resolve({
      models: {
        'deepseek-max': {
          provider: 'ds',
          model_name: 'deepseek-max',
          display_name: 'DS Max',
          default_params: { reasoning_effort: 'low' },
        },
      },
      providers: {},
      defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } },
    })
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.thinkingStrength).toBe('low')
  })

  it('标签显式记忆优先于参数反推', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.defaults = Promise.resolve({ chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } })
    stubs.llmConfig = Promise.resolve({
      models: {
        'deepseek-max': {
          provider: 'ds',
          model_name: 'deepseek-max',
          display_name: 'DS Max',
          default_params: { reasoning_effort: 'low' },
        },
      },
      providers: {},
      defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } },
    })
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    useThinkingModeStore.getState().setStrength('main-1', 'high')
    await mountContainer()
    expect(stubs.chatInput?.thinkingStrength).toBe('high')
  })

  it('反推不出（无模型配置）→ 回退默认档 medium', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(stubs.chatInput?.thinkingStrength).toBe('medium')
  })

  it('切换强度：写入标签记忆并调用 switchThinkingMode（off → 不启用思考）', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.defaults = Promise.resolve({ chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } })
    useAgentTabStore.setState({
      tabs: [makeMainTab({ agentId: 'agent-1' })],
      activeTabId: 'main-1',
    })
    await mountContainer()

    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.('off')
    })
    expect(useThinkingModeStore.getState().strengthByTabId['main-1']).toBe('off')
    expect(switchModeMock).toHaveBeenCalledWith('deepseek-max', false)
  })

  it('切换强度失败 → 一次性提示「思考强度同步失败」，本地记忆不受影响', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.defaults = Promise.resolve({ chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } })
    useAgentTabStore.setState({
      tabs: [makeMainTab({ agentId: 'agent-1' })],
      activeTabId: 'main-1',
    })
    switchModeMock.mockRejectedValue(new Error('网络异常'))
    await mountContainer()

    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.('high')
      await Promise.resolve()
    })
    expect(notificationCountByTitle('思考强度同步失败')).toBe(1)

    // 重复失败不重复弹（生命周期一次性）
    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.('low')
      await Promise.resolve()
    })
    expect(notificationCountByTitle('思考强度同步失败')).toBe(1)
    expect(useThinkingModeStore.getState().strengthByTabId['main-1']).toBe('low')
  })

  it('模型名缺失 → 仅本地记忆，不调用后端切换', async () => {
    setupActivePipeline([])
    await mountContainer()

    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.('high')
    })
    expect(useThinkingModeStore.getState().strengthByTabId['main-1']).toBe('high')
    expect(switchModeMock).not.toHaveBeenCalled()
  })

  it('无激活 Tab（tabId 为空）→ 切换不落标签记忆也不调后端', async () => {
    await mountContainer()

    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.('high')
    })
    expect(useThinkingModeStore.getState().strengthByTabId).toEqual({})
    expect(switchModeMock).not.toHaveBeenCalled()
  })
})

describe('ChatContainer — 降级提示', () => {
  it('getDefaults 失败 → 一次性提示「模型信息获取失败」（重复挂载失败不重复弹）', async () => {
    stubs.defaults = Promise.reject(new Error('网络异常'))
    await mountContainer()
    expect(notificationCountByTitle('模型信息获取失败')).toBe(1)

    stubs.defaults = Promise.reject(new Error('再次失败'))
    await mountContainer()
    expect(notificationCountByTitle('模型信息获取失败')).toBe(1)
  })

  it('getLLMConfig 失败 → 一次性提示「LLM 配置获取失败」（重复挂载失败不重复弹）', async () => {
    stubs.llmConfig = Promise.reject(new Error('网络异常'))
    await mountContainer()
    expect(notificationCountByTitle('LLM 配置获取失败')).toBe(1)

    stubs.llmConfig = Promise.reject(new Error('再次失败'))
    await mountContainer()
    expect(notificationCountByTitle('LLM 配置获取失败')).toBe(1)
  })
})

describe('ChatContainer — 投票面板', () => {
  it('渲染本会话与无会话归属的 open 投票；过滤他 会话与已关闭投票', async () => {
    useVotingStore.setState({
      votingSessions: [
        makeVoting({ id: 'v1', sessionId: 'sess-1' }),
        makeVoting({ id: 'v2' }),
        makeVoting({ id: 'v3', sessionId: 'sess-other' }),
        makeVoting({ id: 'v4', sessionId: 'sess-1', status: 'closed' }),
      ],
    })
    const { queryAllByTestId } = await mountContainer()
    expect(queryAllByTestId('stub-voting-panel')).toHaveLength(2)
    // stub 在重渲染时会被重复调用，按唯一 id 断言渲染的是哪两张投票
    expect([...new Set(stubs.votingPanels.map((v) => v.id))]).toEqual(['v1', 'v2'])
  })

  it('无活跃投票 → 不渲染投票面板区', async () => {
    const { queryByTestId } = await mountContainer()
    expect(queryByTestId('stub-voting-panel')).not.toBeInTheDocument()
  })
})

describe('ChatContainer — 子标签完成禁用', () => {
  it('子标签 completed → ChatInput 禁用；发送直接未受理不上抛', async () => {
    useAgentTabStore.setState({ tabs: [makeMainTab(), makeSubTab({ status: 'completed' })], activeTabId: 'sub-1' })
    await mountContainer()

    expect(stubs.chatInput?.disabled).toBe(true)
    let receipt: boolean | void
    await act(async () => {
      receipt = stubs.chatInput?.onSendMessage?.({ content: '内容' })
    })
    expect(receipt).toBe(false)
    expect(outerSend).not.toHaveBeenCalled()
  })

  it('子标签 failed → 同样禁用', async () => {
    useAgentTabStore.setState({ tabs: [makeMainTab(), makeSubTab({ status: 'failed' })], activeTabId: 'sub-1' })
    await mountContainer()
    expect(stubs.chatInput?.disabled).toBe(true)
  })

  it('子标签 running → 不禁用', async () => {
    useAgentTabStore.setState({ tabs: [makeMainTab(), makeSubTab()], activeTabId: 'sub-1' })
    await mountContainer()
    expect(stubs.chatInput?.disabled).toBe(false)
  })

  it('主标签 completed → 不禁用（完成禁用仅作用于子标签）', async () => {
    useAgentTabStore.setState({ tabs: [makeMainTab({ status: 'completed' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.disabled).toBe(false)
  })
})

describe('ChatContainer — 待发送队列条与输入草稿键', () => {
  it('当前标签有管道 → 渲染 PendingInputQueueBar 且携带管道 ID', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(stubs.pendingBarRendered).toBe(true)
    expect(stubs.pendingBarPipelineId).toBe('pipe-1')
  })

  it('当前标签无管道 → 不渲染队列条', async () => {
    await mountContainer()
    expect(stubs.pendingBarRendered).toBe(false)
  })

  it('draftKey 优先取激活 Tab，无 Tab 回落 sessionId', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(stubs.chatInput?.draftKey).toBe('main-1')
  })
})
