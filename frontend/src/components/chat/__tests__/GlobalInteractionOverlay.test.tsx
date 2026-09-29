// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * GlobalInteractionOverlay 全局交互浮层测试
 *
 * 覆盖：待处理列表过滤（仅 pending）、多卡片导航（上一个/下一个 + 边界禁用 +
 * 索引自动重置）、最小化徽标（含审批倒计时）与恢复、按钮/ESC 关闭、
 * 三类响应回调的参数投影与失败提示、跨卡片提交互斥守卫、底部停靠布局契约
 * （ADR 2026-09-29：底部居中 + 遮罩仅视觉不拦截 + 宽度视口内加宽；
 * BUG-43 右下悬浮锚定方案被否回退）。
 *
 * mock 约定：useInteractionHandler（WebSocket 编排层）、toast、logger 为外部
 * 边界整模块 mock；InteractionCard 以按钮桩替身回放回调参数；两个 zustand
 * store 走真实现。
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { updateSessionsCache } from '@/hooks/queries/useSessionsQuery'
import { useInteractionStore } from '@/stores/interactionStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { GlobalInteractionOverlay } from '../GlobalInteractionOverlay'
import type { PendingInteraction } from '@/stores/interactionStore'
import type { Session } from '@/types/models'

// ---------------------------------------------------------------------------
//  Mock：外部边界
// ---------------------------------------------------------------------------
const handlers = vi.hoisted(() => ({
  respondChoice: vi.fn(),
  respondConversation: vi.fn(),
  navigateToTab: vi.fn(),
}))
vi.mock('@/hooks/useInteractionHandler', () => ({
  useInteractionHandler: () => handlers,
}))

const toastError = vi.hoisted(() => vi.fn())
vi.mock('@/components/ui/sonner', () => ({
  toast: { error: toastError },
}))

const loggerError = vi.hoisted(() => vi.fn())
vi.mock('@/utils/logger', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/utils/logger')>()
  return {
    ...actual,
    logger: { module: () => ({ error: loggerError }) },
  }
})

/** InteractionCard 桩：暴露回调触发按钮与 isSubmitting/requestId/originLabel 可观察面 */
vi.mock('@/components/chat/InteractionCard', () => ({
  InteractionCard: ({
    interaction,
    originLabel,
    onRespondChoice,
    onRespondText,
    onNavigateToTab,
    onDismiss,
    isSubmitting,
  }: {
    interaction: PendingInteraction
    originLabel?: string
    onRespondChoice: (optionId: string, optionLabel?: string) => void
    onRespondText: (text: string) => void
    onNavigateToTab: () => void
    onDismiss: () => void
    isSubmitting: boolean
  }) => (
    <div
      data-testid="interaction-card"
      data-request-id={interaction.requestId}
      data-submitting={String(isSubmitting)}
      data-origin-label={originLabel}
    >
      <span>{interaction.title}</span>
      <button onClick={() => onRespondChoice('opt-1', '批准')}>fire-choice-labeled</button>
      <button onClick={() => onRespondChoice('opt-2')}>fire-choice-id</button>
      <button onClick={() => onRespondText('自定义回复')}>fire-text</button>
      <button onClick={() => onNavigateToTab()}>fire-navigate</button>
      <button onClick={() => onDismiss()}>fire-dismiss</button>
    </div>
  ),
}))

// ---------------------------------------------------------------------------
//  工厂
// ---------------------------------------------------------------------------
function makeInteraction(overrides: Partial<PendingInteraction> = {}): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '审批请求',
    description: '',
    threadId: 'thread-1',
    tabId: 'tab-1',
    agentId: 'agent-1',
    agentLevel: 'L2',
    pipelineId: 'pipeline-1',
    timestamp: '2026-09-13T00:00:00Z',
    status: 'pending',
    options: [{ id: 'opt-1', label: '批准' }],
    ...overrides,
  }
}

function card(): HTMLElement {
  return screen.getByTestId('interaction-card')
}

function setInteractions(items: PendingInteraction[]) {
  useInteractionStore.setState({ pendingInteractions: items })
}

beforeEach(() => {
  handlers.respondChoice.mockReset()
  handlers.respondConversation.mockReset()
  handlers.navigateToTab.mockReset()
  toastError.mockClear()
  loggerError.mockClear()
  useInteractionStore.setState({
    pendingInteractions: [],
    isMinimized: false,
    globalOpenRequestId: null,
  })
  useSessionStore.setState({ activeSessionId: 'sess-1' })
})

describe('GlobalInteractionOverlay 显隐', () => {
  it('无待处理且非最小化：不渲染任何内容', () => {
    const { container } = render(<GlobalInteractionOverlay />)
    expect(container).toBeEmptyDOMElement()
  })

  it('最小化且无待处理：不渲染浮窗按钮', () => {
    useInteractionStore.setState({ isMinimized: true })
    const { container } = render(<GlobalInteractionOverlay />)
    expect(container).toBeEmptyDOMElement()
  })

  it('最小化且有待处理：渲染计数浮窗，点击恢复卡片', async () => {
    setInteractions([makeInteraction(), makeInteraction({ requestId: 'req-2' })])
    useInteractionStore.setState({ isMinimized: true })
    render(<GlobalInteractionOverlay />)

    expect(screen.getByText('2 个待处理交互')).toBeInTheDocument()
    fireEvent.click(screen.getByText('2 个待处理交互'))
    expect(useInteractionStore.getState().isMinimized).toBe(false)
    await waitFor(() => expect(card()).toBeInTheDocument())
  })

  it('仅 pending 状态参与堆叠：responded/dismissed 的不显示也不计数', () => {
    setInteractions([
      makeInteraction(),
      makeInteraction({ requestId: 'req-answered', status: 'responded' }),
      makeInteraction({ requestId: 'req-gone', status: 'dismissed' }),
    ])
    const { container } = render(<GlobalInteractionOverlay />)
    expect(card()).toBeInTheDocument()
    expect(screen.getByText('1 / 1')).toBeInTheDocument()
    expect(screen.queryByText('req-answered')).not.toBeInTheDocument()
    // 卡片经 portal 挂 document.body
    expect(document.body).not.toBeEmptyDOMElement()
    expect(container).toBeEmptyDOMElement()
  })
})

describe('GlobalInteractionOverlay 多卡片导航', () => {
  beforeEach(() => {
    setInteractions([makeInteraction(), makeInteraction({ requestId: 'req-2', title: '澄清问题' })])
  })

  it('首张卡片：上一页禁用、下一页可用，展示当前交互', () => {
    render(<GlobalInteractionOverlay />)
    expect(card().getAttribute('data-request-id')).toBe('req-1')
    expect(screen.getByText('1 / 2')).toBeInTheDocument()
    expect(screen.getByTitle('上一个')).toBeDisabled()
    expect(screen.getByTitle('下一个')).toBeEnabled()
  })

  it('下一页到末尾：末张禁用下一页，上一页可用；返回后索引复原', async () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.click(screen.getByTitle('下一个'))
    expect(card().getAttribute('data-request-id')).toBe('req-2')
    expect(screen.getByText('2 / 2')).toBeInTheDocument()
    expect(screen.getByTitle('下一个')).toBeDisabled()
    expect(screen.getByTitle('上一个')).toBeEnabled()

    fireEvent.click(screen.getByTitle('上一个'))
    expect(card().getAttribute('data-request-id')).toBe('req-1')
    expect(screen.getByText('1 / 2')).toBeInTheDocument()
  })

  it('索引自动重置：当前卡被移除后回到最后一张有效卡', () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.click(screen.getByTitle('下一个'))
    expect(card().getAttribute('data-request-id')).toBe('req-2')

    act(() => {
      useInteractionStore.getState().dismissInteraction('req-2')
    })
    expect(card().getAttribute('data-request-id')).toBe('req-1')
    expect(screen.getByText('1 / 1')).toBeInTheDocument()
  })
})

describe('GlobalInteractionOverlay 关闭与最小化', () => {
  beforeEach(() => {
    setInteractions([makeInteraction()])
  })

  it('ESC 键关闭当前交互；其他按键不关闭', () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(0)
    expect(screen.queryByTestId('interaction-card')).not.toBeInTheDocument()
  })

  it('关闭按钮移除当前交互', () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.click(screen.getByTitle('关闭'))
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(0)
  })

  it('最小化按钮收起为徽标；恢复后徽标点击还原卡片', () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.click(screen.getByTitle('最小化'))
    expect(useInteractionStore.getState().isMinimized).toBe(true)
    expect(screen.getByText('1 个待处理交互')).toBeInTheDocument()

    fireEvent.click(screen.getByText('1 个待处理交互'))
    expect(useInteractionStore.getState().isMinimized).toBe(false)
  })

  // 遮罩仅视觉不拦截的指针分层契约见「浮层底部停靠」组（ADR 2026-09-29）。
})

describe('GlobalInteractionOverlay 响应回调', () => {
  beforeEach(() => {
    setInteractions([makeInteraction()])
  })

  it('选项响应：带 label 时以 label 发送', async () => {
    handlers.respondChoice.mockResolvedValue(undefined)
    render(<GlobalInteractionOverlay />)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-labeled'))
    })
    expect(handlers.respondChoice).toHaveBeenCalledTimes(1)
    expect(handlers.respondChoice).toHaveBeenCalledWith('req-1', '批准')
  })

  it('选项响应：无 label 时回退 optionId', async () => {
    handlers.respondChoice.mockResolvedValue(undefined)
    render(<GlobalInteractionOverlay />)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-id'))
    })
    expect(handlers.respondChoice).toHaveBeenCalledWith('req-1', 'opt-2')
  })

  it('选项响应失败：toast + logger 记录，不崩溃', async () => {
    handlers.respondChoice.mockRejectedValueOnce(new Error('网络断'))
    render(<GlobalInteractionOverlay />)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-labeled'))
    })
    expect(toastError).toHaveBeenCalledWith('交互响应发送失败，请重试')
    expect(loggerError).toHaveBeenCalled()
  })

  it('文字响应成功与失败', async () => {
    handlers.respondConversation.mockResolvedValueOnce(undefined)
    render(<GlobalInteractionOverlay />)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-text'))
    })
    expect(handlers.respondConversation).toHaveBeenCalledWith('req-1', '自定义回复')

    handlers.respondConversation.mockRejectedValueOnce(new Error('网络断'))
    await act(async () => {
      fireEvent.click(screen.getByText('fire-text'))
    })
    expect(toastError).toHaveBeenCalledWith('交互响应发送失败，请重试')
  })

  it('跳转响应：优先 pipelineId；缺省回退 threadId；失败走 toast', async () => {
    handlers.navigateToTab.mockResolvedValue(undefined)
    render(<GlobalInteractionOverlay />)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-navigate'))
    })
    expect(handlers.navigateToTab).toHaveBeenCalledWith(
      'req-1',
      'pipeline-1',
      '审批请求',
      'L2',
    )

    await act(async () => {
      setInteractions([
        makeInteraction({ requestId: 'req-noPipe', pipelineId: undefined, agentLevel: undefined }),
      ])
    })
    handlers.navigateToTab.mockRejectedValueOnce(new Error('网络断'))
    await act(async () => {
      fireEvent.click(screen.getByText('fire-navigate'))
    })
    expect(handlers.navigateToTab).toHaveBeenLastCalledWith(
      'req-noPipe',
      'thread-1',
      '审批请求',
      undefined,
    )
    expect(toastError).toHaveBeenCalledWith('交互响应发送失败，请重试')
  })

  it('提交中状态透传 isSubmitting，完成后复位', async () => {
    let release!: (v: unknown) => void
    handlers.respondChoice.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve }),
    )
    render(<GlobalInteractionOverlay />)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-labeled'))
    })
    expect(card().getAttribute('data-submitting')).toBe('true')

    await act(async () => {
      release(undefined)
    })
    expect(card().getAttribute('data-submitting')).toBe('false')
  })

  it('跨卡片互斥：第一张卡提交未完成时，切到第二张卡的响应被忽略', async () => {
    setInteractions([
      makeInteraction(),
      makeInteraction({ requestId: 'req-2', title: '第二张' }),
    ])
    let release!: (v: unknown) => void
    handlers.respondChoice.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve }),
    )
    render(<GlobalInteractionOverlay />)

    // 第一张卡提交挂起
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-labeled'))
    })
    // 切到第二张卡，尝试响应 → 被 submittingId 守卫拦截（三类响应同受守卫）
    fireEvent.click(screen.getByTitle('下一个'))
    expect(card().getAttribute('data-request-id')).toBe('req-2')
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-labeled'))
      fireEvent.click(screen.getByText('fire-text'))
      fireEvent.click(screen.getByText('fire-navigate'))
    })
    expect(handlers.respondChoice).toHaveBeenCalledTimes(1)
    expect(handlers.respondConversation).not.toHaveBeenCalled()
    expect(handlers.navigateToTab).not.toHaveBeenCalled()

    // 第一张卡的提交完成后，第二张卡恢复可响应
    await act(async () => {
      release(undefined)
    })
    handlers.respondChoice.mockResolvedValueOnce(undefined)
    await act(async () => {
      fireEvent.click(screen.getByText('fire-choice-labeled'))
    })
    expect(handlers.respondChoice).toHaveBeenCalledTimes(2)
    expect(handlers.respondChoice).toHaveBeenLastCalledWith('req-2', '批准')
  })
})

describe('BUG-40 卡片宽度自适应', () => {
  it('浮层宽度视口内加宽：w-full + mx-4 至多 视口-32px，max-w-4xl 封顶', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)
    // 宽度上限容器：w-full + mx-4 保证长选项卡片宽度至多 视口-32px 不横向溢出，
    // max-w-4xl 宽视口封顶（底部停靠居中，ADR 2026-09-29）
    const panel = document.querySelector('[data-testid="interaction-overlay-panel"]')
    expect(panel).not.toBeNull()
    expect(panel!.className).toMatch(/w-full/)
    expect(panel!.className).toMatch(/max-w-4xl/)
    expect(panel!.className).toMatch(/mx-4/)
  })
})

// ---------------------------------------------------------------------------
//  浮层底部停靠（ADR 2026-09-29）
//
//  展开卡停靠底部居中，允许覆盖输入区——遮挡由用户可控手段化解
//  （决策即消 / 收起成徽标 / 关闭 / ESC），不迁移卡片位置；BUG-43 右下
//  悬浮锚定 composer 上缘方案被否回退。非阻断由指针分层保证：外层与
//  遮罩 pointer-events-none（仅视觉半透明），仅卡片容器 pointer-events-auto。
// ---------------------------------------------------------------------------
describe('浮层底部停靠（ADR 2026-09-29）', () => {
  it('展开卡底部停靠居中：容器 inset-x-0 bottom-0，卡片 w-full max-w-4xl，无内联抬升', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)

    const panel = document.querySelector(
      '[data-testid="interaction-overlay-panel"]',
    ) as HTMLElement
    expect(panel.className).toMatch(/w-full/)
    expect(panel.className).toMatch(/max-w-4xl/)
    expect(panel.className).toMatch(/pointer-events-auto/)
    expect(panel.style.bottom).toBe('')

    // 外层容器横贯底部居中，不拦截指针
    const wrapper = panel.parentElement as HTMLElement
    expect(wrapper.className).toMatch(/inset-x-0/)
    expect(wrapper.className).toMatch(/bottom-0/)
    expect(wrapper.className).toMatch(/justify-center/)
    expect(wrapper.className).toMatch(/pointer-events-none/)
  })

  it('遮罩仅视觉半透明不拦截指针：pointer-events-none（侧栏/停止按钮等保持可点）', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)

    const panel = document.querySelector(
      '[data-testid="interaction-overlay-panel"]',
    ) as HTMLElement
    const mask = panel.previousElementSibling as HTMLElement
    expect(mask.className).toMatch(/pointer-events-none/)
    expect(mask.className).toMatch(/overlay-bg/)
  })

  it('卡片高度上限 80vh（底部停靠自留 20vh 头部余量），无内联 maxHeight', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)

    const cardWrap = document.querySelector(
      '[data-testid="interaction-overlay-card"]',
    ) as HTMLElement
    expect(cardWrap.className).toMatch(/max-h-\[80vh\]/)
    expect(cardWrap.style.maxHeight).toBe('')
  })

  it('收起徽标带审批倒计时（N 项待决策 + 剩余时间），点击展开恢复卡片', () => {
    setInteractions([
      makeInteraction({
        // BUG-60 审批族统一 24h：声明 86400s 原样生效——创建于 1s 前 → 剩余 23:59:59（跨秒边界容差 24:00:00）
        timeoutSeconds: 86400,
        createdAt: new Date(Date.now() - 1000).toISOString(),
      }),
    ])
    useInteractionStore.setState({ isMinimized: true })
    render(<GlobalInteractionOverlay />)

    const badge = screen.getByText(/项待决策/)
    expect(badge.textContent).toMatch(/^1 项待决策 (23:59:59|24:00:00)$/)
    fireEvent.click(badge)
    expect(useInteractionStore.getState().isMinimized).toBe(false)
    expect(card()).toBeInTheDocument()
  })

  it('收起徽标右下角停靠（bottom-4 right-4），不占内容区中部', () => {
    setInteractions([makeInteraction({ requestId: 'req-badge' })])
    useInteractionStore.setState({ isMinimized: true })
    render(<GlobalInteractionOverlay />)

    const badge = screen.getByText(/个待处理交互/).parentElement as HTMLElement
    expect(badge.className).toMatch(/bottom-4/)
    expect(badge.className).toMatch(/right-4/)
  })
})
// ---------------------------------------------------------------------------
//  归属标签：多会话/子任务并行时卡片可辨来源（会话标题 · Agent/管道名）
// ---------------------------------------------------------------------------
describe('GlobalInteractionOverlay — 卡片归属标签', () => {
  afterEach(() => {
    usePipelineMessageStore.setState({ pipelines: {} })
    updateSessionsCache(() => [])
  })

  it('会话缓存与管道元数据齐备：originLabel = 会话标题 · Agent 名', () => {
    updateSessionsCache((prev) => [
      ...prev,
      { id: 'sess-origin', title: '帮我看代码' } as unknown as Session,
    ])
    usePipelineMessageStore.setState((s) => ({
      pipelines: {
        ...s.pipelines,
        'pip-origin': {
          pipelineId: 'pip-origin',
          sessionId: 'sess-origin',
          level: 2,
          tabId: null,
          agentName: '子代理A',
          status: 'running',
        },
      },
    }))
    setInteractions([
      makeInteraction({
        requestId: 'req-origin',
        sessionId: 'sess-origin',
        threadId: 'sess-origin',
        pipelineId: 'pip-origin',
      }),
    ])
    render(<GlobalInteractionOverlay />)

    expect(card().getAttribute('data-origin-label')).toBe('帮我看代码 · 子代理A')
  })

  it('会话/管道均解析不到：回退 agentId 原文（与通知 sourceLabel 同一约定）', () => {
    setInteractions([makeInteraction({ requestId: 'req-unknown' })])
    render(<GlobalInteractionOverlay />)

    expect(card().getAttribute('data-origin-label')).toBe('agent-1')
  })

  it('会话标题与 Agent 名同名：去重只显示一份', () => {
    updateSessionsCache((prev) => [
      ...prev,
      { id: 'sess-dup', title: '子代理A' } as unknown as Session,
    ])
    usePipelineMessageStore.setState((s) => ({
      pipelines: {
        ...s.pipelines,
        'pip-dup': {
          pipelineId: 'pip-dup',
          sessionId: 'sess-dup',
          level: 2,
          tabId: null,
          agentName: '子代理A',
          status: 'running',
        },
      },
    }))
    setInteractions([
      makeInteraction({
        requestId: 'req-dup',
        sessionId: 'sess-dup',
        threadId: 'sess-dup',
        pipelineId: 'pip-dup',
      }),
    ])
    render(<GlobalInteractionOverlay />)

    expect(card().getAttribute('data-origin-label')).toBe('子代理A')
  })

  it('交互切换时归属标签跟随当前卡片（act 内换卡同步重渲染）', () => {
    updateSessionsCache((prev) => [
      ...prev,
      { id: 'sess-a', title: '会话甲' } as unknown as Session,
      { id: 'sess-b', title: '会话乙' } as unknown as Session,
    ])
    setInteractions([
      makeInteraction({ requestId: 'req-a', sessionId: 'sess-a', threadId: 'sess-a' }),
      makeInteraction({ requestId: 'req-b', sessionId: 'sess-b', threadId: 'sess-b' }),
    ])
    render(<GlobalInteractionOverlay />)

    expect(card().getAttribute('data-request-id')).toBe('req-a')
    // 无 pipelineId → Agent 名走 agents 缓存未命中回退原文（与通知 sourceLabel 同约定）
    expect(card().getAttribute('data-origin-label')).toBe('会话甲 · agent-1')

    act(() => {
      fireEvent.click(screen.getByTitle('下一个'))
    })
    expect(card().getAttribute('data-request-id')).toBe('req-b')
    expect(card().getAttribute('data-origin-label')).toBe('会话乙 · agent-1')
  })
})
