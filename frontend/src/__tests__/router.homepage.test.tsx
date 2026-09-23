/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * router.tsx HomePage 回调行为测试（与 router.guards / router.lazyRoute 互补：
 * 守卫与路由表在那里，这里经 ChatContainer / ChatPanelShell 探针触发 HomePage
 * 的回调体，断言出站 WS 帧、store 可观测状态与通知副作用）。
 *
 * 打桩边界：
 * - ChatContainer / ChatPanelShell → 探针组件（捕获 props 供测试直呼回调）；
 * - globalWS → 传输层外部依赖，mock 记录出站调用（subscribe 捕获 handler）；
 * - api 传输层 apiClient / createSessionApi / updateSessionApi / fetchPendingInputs
 *   → 网络边界 mock；
 * - performLogout / openWorkspacePanelByPath → 导航副作用 mock；
 * - 其余（zustand stores、resolveSendTarget、appendAttachmentRefs）真实实现，
 *   断言落在 WS 出站帧形状与 store 状态上。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import React from 'react'
import { RouterProvider } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockApiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => {
  const client = {
    get: (...args: unknown[]) => mockApiGet(...args),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  return { default: client, apiClient: client }
})

const mockCreateSession = vi.hoisted(() => vi.fn())
const mockUpdateSession = vi.hoisted(() => vi.fn())
const mockGetMessages = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/session', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  createSession: (...args: unknown[]) => mockCreateSession(...args),
  updateSession: (...args: unknown[]) => mockUpdateSession(...args),
  getMessages: (...args: unknown[]) => mockGetMessages(...args),
}))
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  fetchPendingInputs: vi.fn().mockResolvedValue([]),
}))

const mockWs = vi.hoisted(() => {
  const statusHandlers = new Set<(data: { status: string }) => void>()
  return {
    connect: vi.fn(),
    disconnect: vi.fn(),
    sendActiveThread: vi.fn(),
    sendUserInput: vi.fn(),
    sendCancel: vi.fn(),
    sendRegenerate: vi.fn(),
    sendInteractionResponse: vi.fn(),
    status: 'idle',
    subscribe: vi.fn((_topic: string, handler: (data: { status: string }) => void) => {
      statusHandlers.add(handler)
    }),
    unsubscribe: vi.fn((_topic: string, handler: (data: { status: string }) => void) => {
      statusHandlers.delete(handler)
    }),
    _emitStatus: (status: string) => {
      statusHandlers.forEach((h) => h({ status }))
    },
  }
})
vi.mock('@/services/websocket/GlobalWebSocket', () => ({ globalWS: mockWs }))

const mockPerformLogout = vi.hoisted(() => vi.fn())
vi.mock('@/services/auth/logout', () => ({ performLogout: mockPerformLogout }))
const mockOpenWorkspacePanel = vi.hoisted(() => vi.fn())
vi.mock('@/services/workspacePanelOpener', () => ({
  openWorkspacePanelByPath: mockOpenWorkspacePanel,
}))

// tokenLifecycle：仅替换 ensureFreshToken（发送守卫的自愈原语），其余导出保真。
// 默认恢复失败（resolve null）——未显式设定的用例走「恢复失败」通知分支。
const mockEnsureFreshToken = vi.hoisted(() => vi.fn(() => Promise.resolve(null)))
vi.mock('@/services/auth/tokenLifecycle', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureFreshToken: (...args: unknown[]) => mockEnsureFreshToken(...args),
}))

vi.mock('@lobehub/ui', () => ({}))
vi.mock('@/components/layout/Sidebar', () => ({ Sidebar: () => null }))

type CapturedProps = Record<string, unknown>
let chatProps: CapturedProps | null = null
let shellProps: { chatContent: ReactNode; sidebarContent: ReactNode; onLogout: () => void } | null = null

vi.mock('@/components/chat/ChatContainer', () => ({
  ChatContainer: (props: CapturedProps) => {
    chatProps = props
    return <div data-testid="chat-probe" />
  },
}))
vi.mock('@/components/layout/ChatPanelShell', () => ({
  ChatPanelShell: (props: typeof shellProps) => {
    shellProps = props
    return (
      <div>
        <button data-testid="logout-btn" onClick={() => props?.onLogout()}>退出</button>
        {props?.sidebarContent}
        {props?.chatContent}
      </div>
    )
  },
}))

import { createRouter } from '../router'
import { useAgentTabStore } from '../stores/agentTabStore'
import { useAuthStore } from '../stores/authStore'
import { useInteractionStore, type PendingInteraction } from '../stores/interactionStore'
import { useNotificationStore } from '../stores/notificationStore'
import { usePendingInputStore } from '../stores/pendingInputStore'
import { usePipelineMessageStore } from '../stores/pipelineMessageStore'
import { updateSessionsCache } from '../hooks/queries/useSessionsQuery'
import { saveSessionExecutionOptions } from '../services/sessionExecutionOptions'
import { useSessionListStore } from '../stores/sessionListStore'
import { useSessionStore } from '../stores/sessionStore'
import { useUIStore } from '../stores/uiStore'

// query 钩子挂载即拉数据：api 传输层 mock 为空信封
mockApiGet.mockResolvedValue({ data: { items: [], threads: [], children: [], tree: [], tasks: [] } })

// zustand 5：模块加载时抓各 store 初始态快照，测试间整体复位
const initial = {
  session: useSessionStore.getInitialState(),
  sessionList: useSessionListStore.getInitialState(),
  pipeline: usePipelineMessageStore.getInitialState(),
  interaction: useInteractionStore.getInitialState(),
  pendingInput: usePendingInputStore.getInitialState(),
  agentTab: useAgentTabStore.getInitialState(),
  ui: useUIStore.getInitialState(),
  auth: useAuthStore.getInitialState(),
  notification: useNotificationStore.getInitialState(),
}

/** 渲染 HOME 主页（createRouter + RouterProvider 真渲染），返回卸载函数 */
async function renderHome(): Promise<{ unmount: () => void }> {
  // HOME 在受保护路由内：聚焦主页行为，前置已认证态跳过守卫（无 dev 放行旁路）
  useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
  const router = createRouter()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  // 依活跃会话形态等待：已选中 → ChatContainer 探针；未选中 → 欢迎引导
  if (useSessionStore.getState().activeSessionId) {
    await screen.findByTestId('chat-probe')
  } else {
    await screen.findByText('欢迎使用超级终端')
  }
  return view
}

/** 进入已选会话形态：注入会话/令牌/管道，等待 ChatContainer 探针挂载 */
async function renderHomeWithSession(sessionId = 's1', pipelineId = 'p1'): Promise<void> {
  useAuthStore.setState({ token: 'tok-1', isAuthenticated: true })
  useSessionStore.setState({ activeSessionId: sessionId })
  usePipelineMessageStore.setState((s) => ({
    pipelinesBySession: {
      ...s.pipelinesBySession,
      [sessionId]: [
        { pipelineId, sessionId, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 },
      ],
    },
  }))
  updateSessionsCache(() => [
    { id: sessionId, title: '新会话', updatedAt: new Date().toISOString(), pipelineIds: [pipelineId] } as never,
  ])
  await renderHome()
  await screen.findByTestId('chat-probe')
}

function makeInteraction(partial: Partial<PendingInteraction>): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'conversation',
    title: 't',
    description: 'd',
    threadId: 'th1',
    tabId: 'tab1',
    agentId: 'ag1',
    status: 'pending',
    ...partial,
  } as PendingInteraction
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  chatProps = null
  shellProps = null
  useSessionStore.setState(initial.session, true)
  useSessionListStore.setState(initial.sessionList, true)
  usePipelineMessageStore.setState(initial.pipeline, true)
  useInteractionStore.setState(initial.interaction, true)
  usePendingInputStore.setState(initial.pendingInput, true)
  useAgentTabStore.setState(initial.agentTab, true)
  useUIStore.setState(initial.ui, true)
  useAuthStore.setState(initial.auth, true)
  useNotificationStore.setState(initial.notification, true)
  updateSessionsCache(() => [])
  mockApiGet.mockResolvedValue({ data: { items: [], threads: [], children: [], tree: [], tasks: [] } })
})

describe('HomePage 连接类 effect', () => {
  it('token 响应式订阅：登录 token 就位后自动建立 WS 连接并同步状态', async () => {
    useAuthStore.setState({ token: 'tok-abc' })
    await renderHome()
    await waitFor(() => expect(mockWs.connect).toHaveBeenCalledWith('tok-abc'))
    expect(useSessionStore.getState().wsStatus).toBe(mockWs.status)
  })

  it('活跃会话 effect：activePipelineId 为空时上报 thread 不带管道', async () => {
    useSessionStore.setState({ activeSessionId: 's-only' })
    await renderHome()
    expect(mockWs.sendActiveThread).toHaveBeenCalledWith('s-only', undefined)
  })

  it('活跃会话 effect：活跃管道存在时随上报携带', async () => {
    useSessionStore.setState({ activeSessionId: 's1' })
    usePipelineMessageStore.setState({ activePipelineId: 'p1' })
    await renderHome()
    expect(mockWs.sendActiveThread).toHaveBeenCalledWith('s1', 'p1')
  })

  it('_status 订阅注册 _status 主题回调，回调写 wsStatus；卸载时解除订阅', async () => {
    const { unmount } = await renderHome()
    expect(mockWs.subscribe).toHaveBeenCalledWith('_status', expect.any(Function))
    await act(async () => {
      mockWs._emitStatus('disconnected')
    })
    expect(useSessionStore.getState().wsStatus).toBe('disconnected')
    unmount()
    // 卸载解除的必须是 HomePage 自己注册的那个 handler（按引用退订）
    const [, handler] = mockWs.subscribe.mock.calls.find(([topic]) => topic === '_status') as [
      string,
      (data: { status: string }) => void,
    ]
    expect(mockWs.unsubscribe).toHaveBeenCalledWith('_status', handler)
  })
})

describe('handleCreateSession（欢迎页入口）', () => {
  it('成功路径：建会话 → 自动选中 → 移动端收起侧边栏', async () => {
    mockCreateSession.mockResolvedValue({ id: 's-new', title: '新会话' })
    useAuthStore.setState({ token: 'tok-1' })
    Object.defineProperty(window, 'innerWidth', { value: 480, configurable: true })
    await renderHome()
    fireEvent.click(screen.getByRole('button', { name: /新会话/ }))
    await screen.findByTestId('chat-probe')
    expect(mockCreateSession).toHaveBeenCalledTimes(1)
    expect(useSessionStore.getState().activeSessionId).toBe('s-new')
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  })

  it('桌面端选择会话不收起侧边栏', async () => {
    mockCreateSession.mockResolvedValue({ id: 's-new', title: '新会话' })
    await renderHome()
    fireEvent.click(screen.getByRole('button', { name: /新会话/ }))
    await screen.findByTestId('chat-probe')
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })

  it('失败路径：错误对象带 message → 通知透出该 message', async () => {
    mockCreateSession.mockRejectedValue({ message: '后端炸了' })
    await renderHome()
    fireEvent.click(screen.getByRole('button', { name: /新会话/ }))
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications.some((n) => n.title === '创建会话失败' && n.message === '后端炸了')).toBe(true),
    )
    expect(screen.queryByTestId('chat-probe')).not.toBeInTheDocument()
  })

  it('失败路径：无 message 的异常 → 通知回退通用网络提示', async () => {
    mockCreateSession.mockRejectedValue('boom')
    await renderHome()
    fireEvent.click(screen.getByRole('button', { name: /新会话/ }))
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications.some((n) => n.message === '请检查网络连接后重试')).toBe(true),
    )
  })

  it('欢迎页「浏览智能体」按钮经 workspacePanelOpener 打开面板', async () => {
    await renderHome()
    fireEvent.click(screen.getByRole('button', { name: /浏览智能体/ }))
    expect(mockOpenWorkspacePanel).toHaveBeenCalledWith('/agents')
  })
})

/** 捕获 chatProps 上的 onSendMessage（渲染后由 HomePage 回填） */
function send(params: Record<string, unknown>): unknown {
  return (chatProps as CapturedProps | null)?.onSendMessage?.(params as never)
}

/** act 包裹发送并捕获返回值（静默拒绝点断言统一走 false 契约） */
async function sendAndCapture(params: Record<string, unknown>): Promise<unknown> {
  let result: unknown
  await act(async () => {
    result = send(params)
  })
  return result
}

describe('handleSendMessage', () => {
  it('无活跃会话：返回 false 不出站', async () => {
    await renderHomeWithSession()
    useSessionStore.setState({ activeSessionId: null })
    await act(async () => {
      expect(send({ content: 'hi', pipelineId: 'p1' })).toBe(false)
    })
    expect(mockWs.sendUserInput).not.toHaveBeenCalled()
  })

  it('无令牌：返回 false 不出站', async () => {
    await renderHomeWithSession()
    useAuthStore.setState({ token: null })
    await act(async () => {
      expect(send({ content: 'hi', pipelineId: 'p1' })).toBe(false)
    })
    expect(mockWs.sendUserInput).not.toHaveBeenCalled()
  })

  it('默认标题会话：首条消息改写会话标题（换行折空格、截断 30 字）', async () => {
    await renderHomeWithSession()
    const long = '一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三'
    await sendAndCapture({ content: `  ${long}\n尾  `, pipelineId: 'p1' })
    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalled())
    const [, patch] = mockUpdateSession.mock.calls[0]
    expect((patch as { title: string }).title.length).toBeLessThanOrEqual(30)
    expect((patch as { title: string }).title).not.toContain('\n')
  })

  it('已命名会话不再改写标题', async () => {
    await renderHomeWithSession()
    updateSessionsCache((prev) => prev.map((s) => (s.id === 's1' ? { ...s, title: '正经标题' } : s)))
    await sendAndCapture({ content: 'hi', pipelineId: 'p1' })
    await act(async () => {})
    expect(mockUpdateSession).not.toHaveBeenCalled()
  })

  it('目标管道不属于会话：fail-closed 终止发送并高优通知', async () => {
    await renderHomeWithSession()
    expect(await sendAndCapture({ content: 'hi', pipelineId: 'p-rogue' })).toBe(false)
    expect(mockWs.sendUserInput).not.toHaveBeenCalled()
    expect(useNotificationStore.getState().notifications.some((n) => n.title === '发送已终止')).toBe(true)
  })

  it('标签管道映射成员通过校验：正常放行（成员集 = 映射 ∪ 会话 pipelineIds）', async () => {
    await renderHomeWithSession()
    // pipelineTabMap 以管道 id 为键（resolveSendTarget 成员集取 Object.keys）
    useAgentTabStore.setState({ pipelineTabMap: { 'p-from-tab': 'tab-x' } })
    await sendAndCapture({ content: 'hi', pipelineId: 'p-from-tab' })
    expect(mockWs.sendUserInput).toHaveBeenCalled()
    expect(useNotificationStore.getState().notifications.some((n) => n.title === '发送已终止')).toBe(false)
  })

  it('busy 分支：管道流式中 → 照常出站但不建乐观气泡，待处理队列同步', async () => {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      streamingState: { ...s.streamingState, p1: { isStreaming: true, messageId: 'm-st' } },
    }))
    await sendAndCapture({ content: '排队', pipelineId: 'p1', enableThinking: true, thinkingStrength: 'high' })
    expect(mockWs.sendUserInput).toHaveBeenCalledTimes(1)
    const [, content, opts] = mockWs.sendUserInput.mock.calls[0]
    expect(content).toBe('排队')
    expect(opts).toMatchObject({ pipelineId: 'p1', enableThinking: true, thinkingStrength: 'high' })
    expect((opts as { clientMessageId: string }).clientMessageId).toBeTruthy()
    const pipeline = usePipelineMessageStore.getState()
    expect(pipeline.messagesByPipeline['p1'] ?? []).toHaveLength(0)
    expect(usePendingInputStore.getState().byPipeline['p1']).toBeDefined()
  })

  it('正常分支：乐观 user 消息 + 流式态启动 + 未决 conversation 交互自动解除', async () => {
    await renderHomeWithSession()
    useInteractionStore.setState({
      pendingInteractions: [
        makeInteraction({ requestId: 'req-a', pipelineId: 'p1' }),
        makeInteraction({ requestId: 'req-b', pipelineId: 'p1', status: 'entered' }),
        makeInteraction({ requestId: 'req-c', mode: 'choice', pipelineId: 'p1' }),
        makeInteraction({ requestId: 'req-d', pipelineId: 'p-other' }),
      ],
    })
    await sendAndCapture({
        content: '看图',
        pipelineId: 'p1',
        attachments: [{ url: '/uploads/a.png', name: '截图', type: 'image/png' }],
      })
    const pipeline = usePipelineMessageStore.getState()
    const bucket = pipeline.messagesByPipeline['p1'] ?? []
    expect(bucket).toHaveLength(1)
    expect(bucket[0]).toMatchObject({ role: 'user', status: 'sending', sessionId: 's1' })
    expect(bucket[0].content).toContain('看图')
    expect(bucket[0].content).toContain('![截图](/uploads/a.png)')
    expect(pipeline.streamingState['p1']).toBeDefined()
    // 仅未决 conversation 模式且属于目标管道的交互被解除；choice/他管道不动
    expect(mockWs.sendInteractionResponse).toHaveBeenCalledTimes(2)
    expect(mockWs.sendInteractionResponse).toHaveBeenCalledWith('s1', 'req-a', { response_type: 'approved', feedback: '' })
    const interactions = useInteractionStore.getState().pendingInteractions
    expect(interactions.find((i) => i.requestId === 'req-a')?.status).toBe('responded')
    expect(interactions.find((i) => i.requestId === 'req-c')?.status).toBe('pending')
    expect(mockWs.sendUserInput).toHaveBeenCalledTimes(1)
  })

  it('正常分支：消息级 execution_context 透传出站帧（BUG-35 回归钉）', async () => {
    await renderHomeWithSession()
    await sendAndCapture({ content: '编码', pipelineId: 'p1', mode: 'coding' })
    expect(mockWs.sendUserInput).toHaveBeenCalledTimes(1)
    const [, , opts] = mockWs.sendUserInput.mock.calls[0]
    expect((opts as { executionContext?: Record<string, unknown> }).executionContext).toEqual({
      mode: 'coding',
    })
  })

  it('正常分支：显式模式并入会话执行选项快照，不覆盖其余键（BUG-35 回归钉）', async () => {
    saveSessionExecutionOptions('s1', {
      values: {},
      executionContext: { workspace: { source_path: '/w' } },
    })
    await renderHomeWithSession()
    await sendAndCapture({ content: '编码', pipelineId: 'p1', mode: 'coding' })
    const [, , opts] = mockWs.sendUserInput.mock.calls[0]
    expect((opts as { executionContext?: Record<string, unknown> }).executionContext).toEqual({
      workspace: { source_path: '/w' },
      mode: 'coding',
    })
  })

  it('自动（无 mode）：不携带 mode 键——无快照时 executionContext 缺席（BUG-35 回归钉）', async () => {
    await renderHomeWithSession()
    await sendAndCapture({ content: '自动', pipelineId: 'p1' })
    const [, , opts] = mockWs.sendUserInput.mock.calls[0]
    expect((opts as { executionContext?: Record<string, unknown> }).executionContext).toBeUndefined()
  })

  it('自动（无 mode）：有快照时快照原样透出，帧内无 mode 键（BUG-35 回归钉）', async () => {
    saveSessionExecutionOptions('s1', {
      values: {},
      executionContext: { workspace: { source_path: '/w' } },
    })
    await renderHomeWithSession()
    await sendAndCapture({ content: '自动', pipelineId: 'p1' })
    const [, , opts] = mockWs.sendUserInput.mock.calls[0]
    expect((opts as { executionContext?: Record<string, unknown> }).executionContext).toEqual({
      workspace: { source_path: '/w' },
    })
  })

  it('busy 分支：消息级 execution_context 同样透传（BUG-35 回归钉）', async () => {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      streamingState: { ...s.streamingState, p1: { isStreaming: true, messageId: 'm-st' } },
    }))
    await sendAndCapture({ content: '排队', pipelineId: 'p1', mode: 'writing' })
    expect(mockWs.sendUserInput).toHaveBeenCalledTimes(1)
    const [, , opts] = mockWs.sendUserInput.mock.calls[0]
    expect((opts as { executionContext?: Record<string, unknown> }).executionContext).toEqual({
      mode: 'writing',
    })
  })
})

describe('handleSendMessage 静默拒绝点显式化（BUG-28 第三刀）', () => {
  // 隔离铠甲：用例中途断言失败也要还回真实时钟（假时钟泄漏会挂掉后续用例）
  afterEach(() => {
    vi.useRealTimers()
  })

  // 真机取证（R69 + 第三刀分支模拟）：router 守卫 `!sid || !currentToken` 曾是
  // 发送链唯一无通知的静默拒绝点——输入保留、无气泡、零通知、零内核痕迹，
  // 用户视角 = 「点了没反应」。契约：拒绝必须显式；token 缺失先自愈
  // （tokenLifecycle 唯一真值源：内存有效令牌直接取用/过期 refresh 轮换），
  // 恢复成功回写 authStore 供用户重发过闸；会话缺失无自愈源，显式引导刷新。

  it('无令牌且自动恢复成功：返回 false 不出站，回写恢复的令牌并通知重发', async () => {
    await renderHomeWithSession()
    mockEnsureFreshToken.mockResolvedValue('tok-renewed')
    useAuthStore.setState({ token: null })
    expect(await sendAndCapture({ content: 'hi', pipelineId: 'p1' })).toBe(false)
    expect(mockWs.sendUserInput).not.toHaveBeenCalled()
    // 自愈回写：tokenLifecycle 真值落 authStore，用户重发即过闸
    await waitFor(() => expect(useAuthStore.getState().token).toBe('tok-renewed'))
    expect(useNotificationStore.getState().notifications.some((n) => n.title === '登录态已恢复')).toBe(true)
  })

  it('无令牌且自动恢复失败：返回 false 不出站，显式通知重新登录', async () => {
    await renderHomeWithSession()
    // 隔离铠甲：通知 store 的 30s 内容指纹去重表是模块级状态（setState 不清，
    // BUG-74 eecf5629a），前面「无令牌：返回 false 不出站」走的也是恢复失败
    // 分支、已入列同 title+message 指纹的通知——把时钟推过去重窗，用例与
    // 文件内顺序解耦（同 useRealtimeEventsBranches / ChatContainer.sendGuard 做法）。
    vi.useFakeTimers({ now: Date.now() + 31_000 })
    mockEnsureFreshToken.mockResolvedValue(null)
    useAuthStore.setState({ token: null })
    expect(await sendAndCapture({ content: 'hi', pipelineId: 'p1' })).toBe(false)
    expect(mockWs.sendUserInput).not.toHaveBeenCalled()
    // 通知在 ensureFreshToken().then 微任务回调里入列：排空后直断
    // （waitFor 的轮询定时器在假时钟下冻结，不适用）
    await act(async () => {})
    expect(
      useNotificationStore.getState().notifications.some((n) => n.title === '发送未受理' && n.message.includes('重新登录')),
    ).toBe(true)
    // 恢复失败不得伪造令牌（诚实状态机）
    expect(useAuthStore.getState().token).toBeNull()
  })

  it('无活跃会话：返回 false 不出站且显式通知引导刷新（不再静默）', async () => {
    await renderHomeWithSession()
    // 隔离铠甲：同上——前面「无活跃会话：返回 false 不出站」已入列同指纹的
    // 「发送未受理」通知，时钟推过去重窗解除文件内顺序耦合。
    vi.useFakeTimers({ now: Date.now() + 31_000 })
    useSessionStore.setState({ activeSessionId: null })
    expect(await sendAndCapture({ content: 'hi', pipelineId: 'p1' })).toBe(false)
    expect(mockWs.sendUserInput).not.toHaveBeenCalled()
    // 无会话分支同步入列，直断即可（假时钟下 waitFor 轮询定时器冻结）
    expect(useNotificationStore.getState().notifications.some((n) => n.title === '发送未受理')).toBe(true)
  })
})

describe('handleStopGenerate', () => {
  it('活跃管道存在：sendCancel 携带该管道并停止其流式态', async () => {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      streamingState: { ...s.streamingState, p1: { isStreaming: true, messageId: 'm' } },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onStopGenerate?.()
    })
    expect(mockWs.sendCancel).toHaveBeenCalledWith('s1', undefined, 'p1')
    expect(usePipelineMessageStore.getState().streamingState['p1']).toBeUndefined()
  })

  it('无活跃管道：兜底清理全部残留流式态', async () => {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      streamingState: {
        ...s.streamingState,
        'p-a': { isStreaming: true, messageId: 'm1' },
        'p-b': { isStreaming: true, messageId: 'm2' },
      },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onStopGenerate?.()
    })
    expect(mockWs.sendCancel).toHaveBeenCalledWith('s1', undefined, undefined)
    expect(usePipelineMessageStore.getState().streamingState).toEqual({})
  })
})

describe('handleRegenerate / onRollbackTo / onEdit', () => {
  async function seedMessages(): Promise<void> {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      messagesByPipeline: {
        ...s.messagesByPipeline,
        p1: [
          { id: 'u1', sessionId: 's1', role: 'user', content: '第一问', timestamp: 't', status: 'sent' },
          { id: 'a1', sessionId: 's1', role: 'assistant', content: '答一', timestamp: 't', status: 'sent' },
          { id: 'u2', sessionId: 's1', role: 'user', content: '第二问', timestamp: 't', status: 'sent' },
          { id: 'a2', sessionId: 's1', role: 'assistant', content: '答二', timestamp: 't', status: 'sent' },
        ],
      },
    }))
  }

  it('regenerate：无活跃管道时不出站', async () => {
    await renderHomeWithSession()
    await act(async () => {
      (chatProps as CapturedProps | null)?.onRegenerate?.()
    })
    expect(mockWs.sendRegenerate).not.toHaveBeenCalled()
  })

  it('regenerate：截断到最后一条 user 之后并出站重跑', async () => {
    await seedMessages()
    await act(async () => {
      (chatProps as CapturedProps | null)?.onRegenerate?.()
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1' })
    const rest = usePipelineMessageStore.getState().messagesByPipeline['p1']
    expect(rest.map((m) => m.id)).toEqual(['u1', 'a1', 'u2'])
  })

  it('regenerate：消息桶里没有 user 消息时不截断不出站', async () => {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      messagesByPipeline: {
        ...s.messagesByPipeline,
        p1: [{ id: 'a1', sessionId: 's1', role: 'assistant', content: '答', timestamp: 't', status: 'sent' }],
      },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onRegenerate?.()
    })
    expect(mockWs.sendRegenerate).not.toHaveBeenCalled()
  })

  it('rollback：截断到指定 user 消息并携带 userMessageId 重跑', async () => {
    await seedMessages()
    await act(async () => {
      (chatProps as CapturedProps | null)?.onRollbackTo?.('u1')
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1', userMessageId: 'u1' })
    expect(usePipelineMessageStore.getState().messagesByPipeline['p1']).toHaveLength(1)
  })

  it('edit：改写目标消息内容并携带 newContent 重跑', async () => {
    await seedMessages()
    await act(async () => {
      await (chatProps as CapturedProps | null)?.onEdit?.('u1', '改后的第一问')
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1', userMessageId: 'u1', newContent: '改后的第一问' })
    const bucket = usePipelineMessageStore.getState().messagesByPipeline['p1']
    expect(bucket.find((m) => m.id === 'u1')?.content).toBe('改后的第一问')
  })
})

describe('回退/编辑重发出站 id 键空间（BUG-57 回归钉）', () => {
  // 双字段范式：user 消息 UI 寻址 id 是前端 uuid，后端权威主键在 recordId
  // （mc_ 指纹，认领时写入；历史回读消息无 recordId，其 id 本身即后端
  // record_id）。内核 regenerate 按 record_id 键空间精确匹配——出站必须带
  // 权威 id；本地截断/改写仍按 UI id 寻址不变。
  async function seedClaimed(): Promise<void> {
    await renderHomeWithSession()
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      messagesByPipeline: {
        ...s.messagesByPipeline,
        p1: [
          { id: 'uuid-a', sessionId: 's1', role: 'user', content: '第一问', timestamp: 't', status: 'sent', recordId: 'mc-aaa' },
          { id: 'ans-a', sessionId: 's1', role: 'assistant', content: '答一', timestamp: 't', status: 'sent' },
          { id: 'uuid-b', sessionId: 's1', role: 'user', content: '第二问', timestamp: 't', status: 'sent', recordId: 'mc-bbb' },
          { id: 'ans-b', sessionId: 's1', role: 'assistant', content: '答二', timestamp: 't', status: 'sent' },
        ],
      },
    }))
  }

  it('回退已认领消息：出站带 recordId（非 UI uuid），本地截断仍按 UI id', async () => {
    await seedClaimed()
    await act(async () => {
      (chatProps as CapturedProps | null)?.onRollbackTo?.('uuid-a')
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1', userMessageId: 'mc-aaa' })
    expect(usePipelineMessageStore.getState().messagesByPipeline['p1']).toHaveLength(1)
  })

  it('回退未认领消息（历史回读形态 id 即 record_id）：出站原样带该 id', async () => {
    await seedClaimed()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: {
        ...s.messagesByPipeline,
        p1: [{ id: 'mc-legacy', sessionId: 's1', role: 'user', content: '旧问', timestamp: 't', status: 'sent' }],
      },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onRollbackTo?.('mc-legacy')
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1', userMessageId: 'mc-legacy' })
  })

  it('编辑重发已认领消息：出站带 recordId + newContent，本地改写仍按 UI id', async () => {
    await seedClaimed()
    await act(async () => {
      await (chatProps as CapturedProps | null)?.onEdit?.('uuid-a', '改写后的第一问')
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1', userMessageId: 'mc-aaa', newContent: '改写后的第一问' })
    const bucket = usePipelineMessageStore.getState().messagesByPipeline['p1']
    expect(bucket.find((m) => m.id === 'uuid-a')?.content).toBe('改写后的第一问')
  })

  it('编辑重发未认领消息（无 recordId）：出站回退 UI id', async () => {
    await seedClaimed()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: {
        ...s.messagesByPipeline,
        p1: [{ id: 'uuid-b', sessionId: 's1', role: 'user', content: '第二问', timestamp: 't', status: 'sent' }],
      },
    }))
    await act(async () => {
      await (chatProps as CapturedProps | null)?.onEdit?.('uuid-b', '改写后的第二问')
    })
    expect(mockWs.sendRegenerate).toHaveBeenCalledWith('s1', { pipelineId: 'p1', userMessageId: 'uuid-b', newContent: '改写后的第二问' })
  })
})

describe('onLoadMoreMessages（向上翻页守卫）', () => {
  // 渲染期 query 钩子（sessions/agents/longTermTasks）会调 mockApiGet；
  // 触发回调前清空调用记录，断言只看回调自身的出站
  async function renderAndClear(): Promise<void> {
    await renderHomeWithSession()
    mockApiGet.mockClear()
  }

  it('无活跃管道：不发请求', async () => {
    await renderAndClear()
    await act(async () => {
      (chatProps as CapturedProps | null)?.onLoadMoreMessages?.()
    })
    expect(mockApiGet).not.toHaveBeenCalled()
  })

  it('已到顶（无更早消息）：不发请求', async () => {
    await renderAndClear()
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      hasMoreOlderByPipeline: { ...s.hasMoreOlderByPipeline, p1: false },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onLoadMoreMessages?.()
    })
    expect(mockApiGet).not.toHaveBeenCalled()
  })

  it('翻页在途：不重复发请求', async () => {
    await renderAndClear()
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      hasMoreOlderByPipeline: { ...s.hasMoreOlderByPipeline, p1: true },
      isLoadingOlderByPipeline: { ...s.isLoadingOlderByPipeline, p1: true },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onLoadMoreMessages?.()
    })
    expect(mockApiGet).not.toHaveBeenCalled()
  })

  it('正常翻页：携带 before_sequence 游标拉取更早消息', async () => {
    await renderAndClear()
    mockGetMessages.mockResolvedValue({ messages: [], has_more: false })
    usePipelineMessageStore.setState((s) => ({
      activePipelineId: 'p1',
      hasMoreOlderByPipeline: { ...s.hasMoreOlderByPipeline, p1: true },
      topCursorsByPipeline: { ...s.topCursorsByPipeline, p1: 42 },
    }))
    await act(async () => {
      (chatProps as CapturedProps | null)?.onLoadMoreMessages?.()
    })
    await waitFor(() => expect(mockGetMessages).toHaveBeenCalled())
    const [tid, opts] = mockGetMessages.mock.calls[mockGetMessages.mock.calls.length - 1]
    // 顶部游标原样透传为 before_sequence（游标 = 已加载最小 sequence，向更早翻页）
    expect(opts).toMatchObject({ before_sequence: 42, pipelineRunId: 'p1' })
    expect(tid).toBe('s1')
  })
})

describe('handleLogout', () => {
  it('登出收敛到 performLogout 并携带导航函数', async () => {
    await renderHome()
    fireEvent.click(screen.getByTestId('logout-btn'))
    await waitFor(() => expect(mockPerformLogout).toHaveBeenCalledTimes(1))
    expect(typeof mockPerformLogout.mock.calls[0][0]).toBe('function')
  })
})
