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
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThinkingModeStore } from '@/stores/thinkingModeStore'
import { useUIStore } from '@/stores/uiStore'
import { useVotingStore } from '@/stores/votingStore'
import { ChatContainer } from '../ChatContainer'
import type { ChatContainerProps, SendMessageParams } from '../types'
import type { PipelineMeta } from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'
import type { AgentTab } from '@/types/task'
import type { VotingSession } from '@/types/voting'

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
    thinkingStrength?: string
    onThinkingStrengthChange?: (strength: string) => void
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
  runs: {} as Record<string, { status: string; title?: string }>,
  llmView: null as null | {
    models: Record<string, { provider: string; model_name: string; display_name: string; default_params?: Record<string, unknown> }>
    providers: Record<string, unknown>
    defaults: { chat: string; embedding: string; tiers: Record<string, string> }
  },
  llmError: false as boolean,
  thinkingLevels: null as null | {
    model: string
    fields: Array<Record<string, unknown>>
    options: Array<{ value: string; label: string }>
    current: string | null
  },
}))


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
vi.mock('../ReferenceSelectionRow', () => ({
  ReferenceSelectionRow: () => <div data-testid="stub-reference-row" />,
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
vi.mock('@/hooks/queries/useLlmQueries', () => ({
  useLlmConfigQuery: () => ({ data: stubs.llmView, isError: stubs.llmError }),
  useThinkingLevelsQuery: () => ({ data: stubs.thinkingLevels }),
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
  stubs.llmView = {
    models: {},
    providers: {},
    defaults: { chat: 'chat-model', embedding: 'emb-model', tiers: {} },
  }
  stubs.llmError = false
  stubs.thinkingLevels = null
  localStorage.clear()
  outerSend.mockClear()
  useUIStore.setState({ messageSearchQuery: '', messageJump: null })
  useNotificationStore.setState({ notifications: [] })
  usePipelineMessageStore.setState({
    activePipelineId: null,
    messagesByPipeline: {},
    pipelines: {},
    streamingState: {},
    roundQueueDepthByPipeline: {},
    stoppingByPipeline: {},
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
  it('标题取管道标题（runs.title）；无标题时主 Tab 落「灵汐」、子 Tab 落「未命名任务」，不落 agentName', async () => {
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
      name: '灵汐',
      agentName: '灵汐',
      status: 'running',
      isActive: true,
      unreadCount: 3,
      canClose: false,
      agentLevel: 1,
    })
    // 子 Tab 的 agentName='子任务' 是开签快照里的 agent 侧词汇，不得占标题位
    expect(subBar).toMatchObject({
      id: 'sub-1',
      name: '未命名任务',
      agentName: '未命名任务',
      isActive: false,
      unreadCount: 0,
      canClose: true,
      agentLevel: 2,
    })
  })

  it('管道标题在场时标签名 = 管道标题（主/子同权），agentName 只留悬浮提示', async () => {
    useAgentTabStore.setState({
      tabs: [
        makeMainTab({ agentName: '会话旧名' }),
        makeSubTab({ agentName: '交互工具' }),
      ],
      activeTabId: 'main-1',
    })
    stubs.runs = {
      'pipe-1': { status: 'running', title: 'DSH 生态兼容可行性调研' },
      'pipe-sub-1': { status: 'running', title: 'ZCode 右上角悬浮窗调研' },
    }
    await mountContainer()

    const [mainBar, subBar] = stubs.tabBar?.tabs ?? []
    expect(mainBar).toMatchObject({ name: 'DSH 生态兼容可行性调研' })
    // 复现缺陷场景：agentName 被写成工具标题（'交互工具'），标题位必须仍是管道标题
    expect(subBar).toMatchObject({ name: 'ZCode 右上角悬浮窗调研' })
  })

  it('管道标题为空串时不冒充标题，落降级名（不回落 agentName）', async () => {
    useAgentTabStore.setState({
      tabs: [makeSubTab({ agentName: '交互工具' })],
      activeTabId: 'sub-1',
    })
    stubs.runs = { 'pipe-sub-1': { status: 'running', title: '' } }
    await mountContainer()

    expect(stubs.tabBar?.tabs[0]).toMatchObject({ name: '未命名任务' })
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

describe('ChatContainer — Tab 状态权威投影（runs 快照覆盖开签快照）', () => {
  /** 用户实证场景（2026-11-07）：管道恢复运行后标签仍显示旧终态 ✕——
   *  store 快照开签后无回写，标签状态须以 runs 权威态（任务管理页同源）
   *  实时投影覆盖。 */
  it('快照 failed 而 runs 行 running → 标签显示 running（错位修复正向）', async () => {
    useAgentTabStore.setState({
      tabs: [makeSubTab({ status: 'failed' })],
      activeTabId: 'sub-1',
    })
    stubs.runs = { 'pipe-sub-1': { status: 'running' } }
    await mountContainer()

    expect(stubs.tabBar?.tabs[0]).toMatchObject({ id: 'sub-1', status: 'running' })
  })

  it('runs 无行时保持 store 快照（缺数据不猜，failed 不误翻绿）', async () => {
    useAgentTabStore.setState({
      tabs: [makeSubTab({ status: 'failed' })],
      activeTabId: 'sub-1',
    })
    stubs.runs = {}
    await mountContainer()

    expect(stubs.tabBar?.tabs[0]).toMatchObject({ id: 'sub-1', status: 'failed' })
  })

  it.each([
    ['completed', 'completed'],
    ['suspended', 'waiting_input'],
    ['cancelled', 'waiting_input'],
    ['failed', 'failed'],
  ] as const)('runs 态 %s → 标签态 %s（投影词表）', async (runStatus, tabStatus) => {
    useAgentTabStore.setState({
      tabs: [makeSubTab({ status: 'running' })],
      activeTabId: 'sub-1',
    })
    stubs.runs = { 'pipe-sub-1': { status: runStatus } }
    await mountContainer()

    expect(stubs.tabBar?.tabs[0]).toMatchObject({ id: 'sub-1', status: tabStatus })
  })

  it('主标签同权投影：快照 running 而 runs 行 failed → 主标签 failed', async () => {
    useAgentTabStore.setState({
      tabs: [makeMainTab()],
      activeTabId: 'main-1',
    })
    stubs.runs = { 'pipe-1': { status: 'failed' } }
    await mountContainer()

    expect(stubs.tabBar?.tabs[0]).toMatchObject({ id: 'main-1', status: 'failed' })
  })

  it('无 pipelineRunId 的 Tab 不投影（保持快照，防御脏数据）', async () => {
    useAgentTabStore.setState({
      tabs: [makeSubTab({ pipelineRunId: undefined, status: 'completed' })],
      activeTabId: 'sub-1',
    })
    stubs.runs = { 'pipe-sub-1': { status: 'running' } }
    await mountContainer()

    expect(stubs.tabBar?.tabs[0]).toMatchObject({ id: 'sub-1', status: 'completed' })
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

describe('ChatContainer — 生成态单一真值（runs 快照 + 收尾快照）', () => {
  it('run 结束但 queue_depth>0（链尾 drain 未完成）→ ChatInput/MessageList isGenerating=true', async () => {
    setupActivePipeline([])
    stubs.runs = { 'pipe-1': { status: 'completed' } }
    usePipelineMessageStore.setState({ roundQueueDepthByPipeline: { 'pipe-1': 2 } })
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(true)
    expect(stubs.messageList?.isGenerating).toBe(true)
  })

  it('点停受理中（stoppingLive）→ 生成观感立即让位 isGenerating=false（不等 round_finished）', async () => {
    // 点停即时反馈（2026-11-07）：停止受理期间转圈/生成动画立即让位，发送按钮
    // 恢复可用；runs 快照的 running/suspended 真值不再把停止窗口顶回生成态。
    setupActivePipeline([])
    stubs.runs = { 'pipe-1': { status: 'running' } }
    usePipelineMessageStore.setState({ stoppingByPipeline: { 'pipe-1': true } })
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(false)
    expect(stubs.messageList?.isGenerating).toBe(false)

    // 区分度输入：run_status 已 suspended（后端受理停止、引擎未退出）→ 同样让位
    stubs.runs = { 'pipe-1': { status: 'suspended' } }
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(false)
  })

  it('点停受理中压住 runs running 与队列深度；护栏解除（stopping 清）后恢复生成观感（可辨别、可再点）', async () => {
    setupActivePipeline([])
    stubs.runs = { 'pipe-1': { status: 'running' } }
    usePipelineMessageStore.setState({
      roundQueueDepthByPipeline: { 'pipe-1': 2 },
      stoppingByPipeline: { 'pipe-1': true },
    })
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(false)

    // 护栏 TTL 过期仍无终态事件：stopping 被翻转解除（翻转机制见
    // streaming handlers stopImmediateFeedback.test.ts）→ 生成观感回落 runs
    // 真值：run 仍在跑则转圈恢复，停止未生效可辨别、可再点，不静默装作已停。
    await act(async () => {
      usePipelineMessageStore.setState({ stoppingByPipeline: {} })
    })
    expect(stubs.chatInput?.isGenerating).toBe(true)
  })

  it('runs 已终态 + stopping 残留 → isGenerating=false（终止事件丢失的轮询兜底）', async () => {
    setupActivePipeline([])
    stubs.runs = { 'pipe-1': { status: 'completed' } }
    usePipelineMessageStore.setState({ stoppingByPipeline: { 'pipe-1': true } })
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(false)
  })

  it('流式未激活但 runs 快照 running → 仍视为生成中', async () => {
    setupActivePipeline([])
    stubs.runs = { 'pipe-1': { status: 'running' } }
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(true)
  })

  it('三来源皆静默 → isGenerating=false', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(stubs.chatInput?.isGenerating).toBe(false)
  })
})

describe('ChatContainer — 模型名解析', () => {
  it('agentId 命中 agent.model，经 tiers 映射为显示名', async () => {
    stubs.agents = [{ id: 'agent-1', configId: 'cfg-1', model: 'large', config: {} }]
    stubs.llmView = { models: {}, providers: {}, defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } } }
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('deepseek-max')
  })

  it('agentId 为空时按管道 agentName 兜底解析', async () => {
    stubs.agents = [{ id: 'agent-9', configId: 'planner-agent', model: 'medium', config: {} }]
    stubs.llmView = { models: {}, providers: {}, defaults: { chat: 'c', embedding: 'e', tiers: { medium: 'glm-air' } } }
    usePipelineMessageStore.setState({ pipelines: { 'pipe-1': makePipelineMeta({ agentName: 'planner-agent' }) } })
    useAgentTabStore.setState({ tabs: [makeMainTab()], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('glm-air')
  })

  it('无任何匹配 → 回退系统默认对话模型（defaults.chat）', async () => {
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('chat-model')
  })

  it('agentId 命中但 agent 无任何模型字段 → 回退系统默认对话模型（defaults.chat）', async () => {
    stubs.agents = [{ id: 'agent-1', configId: 'cfg-1' }]
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('chat-model')
  })

  it('llm 配置整体缺失（无 agent 头也无 defaults）→ modelName 为空串（占位语义保留）', async () => {
    stubs.llmView = null
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('')
  })

  it('agent.model 缺失时回退 agent.config.model', async () => {
    stubs.agents = [{ id: 'agent-1', configId: 'cfg-1', config: { model: 'small' } }]
    stubs.llmView = { models: {}, providers: {}, defaults: { chat: 'c', embedding: 'e', tiers: { small: 'glm-air' } } }
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.modelName).toBe('glm-air')
  })
})

const HIGH = '{"reasoning_effort":"max"}'
const LOW = '{"reasoning_effort":"low"}'
const OFF = '{"thinking":{"type":"disabled"}}'

describe('ChatContainer — 思考选择（宿主零业务知识，选项/当前值由端点下发）', () => {
  it('未显式设置 → 显示端点下发的当前参数组（current）', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.llmView = { models: {}, providers: {}, defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } } }
    stubs.thinkingLevels = {
      model: 'deepseek-max',
      fields: [],
      options: [{ value: LOW, label: 'reasoning_effort=low' }],
      current: LOW,
    }
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    await mountContainer()
    expect(stubs.chatInput?.thinkingStrength).toBe(LOW)
  })

  it('标签显式记忆 ∈ 选项时优先于端点当前值', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.llmView = { models: {}, providers: {}, defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } } }
    stubs.thinkingLevels = {
      model: 'deepseek-max',
      fields: [],
      options: [
        { value: LOW, label: 'reasoning_effort=low' },
        { value: HIGH, label: 'reasoning_effort=max' },
      ],
      current: LOW,
    }
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    useThinkingModeStore.getState().setStrength('main-1', HIGH)
    await mountContainer()
    expect(stubs.chatInput?.thinkingStrength).toBe(HIGH)
  })

  it('端点数据未达（无 current）→ 显示空（未选择，发送不覆盖参数）', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(stubs.chatInput?.thinkingStrength).toBe('')
  })

  it('显式记忆不在下发选项内（如换模型后旧记忆失效）→ 回落端点当前值', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.llmView = {
      models: {},
      providers: {},
      defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } },
    }
    stubs.thinkingLevels = {
      model: 'deepseek-max',
      fields: [],
      options: [
        { value: HIGH, label: 'reasoning_effort=max' },
        { value: OFF, label: 'thinking={"type":"disabled"}' },
      ],
      current: HIGH,
    }
    useAgentTabStore.setState({ tabs: [makeMainTab({ agentId: 'agent-1' })], activeTabId: 'main-1' })
    useThinkingModeStore.getState().setStrength('main-1', LOW)
    await mountContainer()
    // LOW 不在下发选项 {HIGH, OFF} 内 → 回落端点当前参数组
    expect(stubs.chatInput?.thinkingStrength).toBe(HIGH)
  })

  it('切换选择：写入标签记忆（参数组随消息级 thinking_strength 透传，无后端伴随调用）', async () => {
    stubs.agents = [{ id: 'agent-1', model: 'large', config: {} }]
    stubs.llmView = { models: {}, providers: {}, defaults: { chat: 'c', embedding: 'e', tiers: { large: 'deepseek-max' } } }
    useAgentTabStore.setState({
      tabs: [makeMainTab({ agentId: 'agent-1' })],
      activeTabId: 'main-1',
    })
    await mountContainer()

    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.(OFF)
    })
    expect(useThinkingModeStore.getState().strengthByTabId['main-1']).toBe(OFF)
  })

  it('无激活 Tab（tabId 为空）→ 切换不落标签记忆', async () => {
    await mountContainer()

    await act(async () => {
      stubs.chatInput?.onThinkingStrengthChange?.(HIGH)
    })
    expect(useThinkingModeStore.getState().strengthByTabId).toEqual({})
  })
})

describe('ChatContainer — 降级提示', () => {
  it('LLM 配置查询失败 → 一次性提示「模型配置获取失败」（重复挂载失败不重复弹）', async () => {
    stubs.llmError = true
    await mountContainer()
    expect(notificationCountByTitle('模型配置获取失败')).toBe(1)

    await mountContainer()
    expect(notificationCountByTitle('模型配置获取失败')).toBe(1)
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

describe('对话标签行 portal（顶带唯一拖拽容器，标签行为其 DOM 子元素）', () => {
  it('槽位存在 → 标签行 portal 进槽位（拖拽容器子元素=自动 no-drag 洞）', async () => {
    const slot = document.createElement('div')
    slot.id = 'chat-top-band-tabs'
    document.body.appendChild(slot)
    try {
      setupActivePipeline([])
      await mountContainer()
      expect(slot.querySelector('[data-testid="stub-tab-bar"]')).not.toBeNull()
      expect(document.querySelector('[data-testid="chat-session-header"]')).toBeNull()
    } finally {
      slot.remove()
    }
  })

  it('槽位不存在（移动端/无顶带形态）→ 标签行内联回退渲染', async () => {
    setupActivePipeline([])
    await mountContainer()
    expect(document.querySelector('[data-testid="stub-tab-bar"]')).not.toBeNull()
  })
})
