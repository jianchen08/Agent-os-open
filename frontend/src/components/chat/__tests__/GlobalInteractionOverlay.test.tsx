// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * GlobalInteractionOverlay 全局交互浮层测试
 *
 * 覆盖：待处理列表过滤（仅 pending）、多卡片导航（上一个/下一个 + 边界禁用 +
 * 索引自动重置）、最小化浮窗与恢复、遮罩/按钮/ESC 关闭、三类响应回调的参数
 * 投影与失败提示、跨卡片提交互斥守卫。
 *
 * mock 约定：useInteractionHandler（WebSocket 编排层）、toast、logger 为外部
 * 边界整模块 mock；InteractionCard 以按钮桩替身回放回调参数；两个 zustand
 * store 走真实现。
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GlobalInteractionOverlay } from '../GlobalInteractionOverlay'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import type { PendingInteraction } from '@/stores/interactionStore'

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

/** InteractionCard 桩：暴露回调触发按钮与 isSubmitting/requestId 可观察面 */
vi.mock('@/components/chat/InteractionCard', () => ({
  InteractionCard: ({
    interaction,
    onRespondChoice,
    onRespondText,
    onNavigateToTab,
    onDismiss,
    isSubmitting,
  }: {
    interaction: PendingInteraction
    onRespondChoice: (optionId: string, optionLabel?: string) => void
    onRespondText: (text: string) => void
    onNavigateToTab: () => void
    onDismiss: () => void
    isSubmitting: boolean
  }) => (
    <div data-testid="interaction-card" data-request-id={interaction.requestId} data-submitting={String(isSubmitting)}>
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

  it('最小化按钮收起为浮窗；恢复后浮窗点击还原卡片', () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.click(screen.getByTitle('最小化'))
    expect(useInteractionStore.getState().isMinimized).toBe(true)
    expect(screen.getByText('1 个待处理交互')).toBeInTheDocument()

    fireEvent.click(screen.getByText('1 个待处理交互'))
    expect(useInteractionStore.getState().isMinimized).toBe(false)
  })

  it('遮罩仅视觉半透明不拦截指针（BUG-14）：点击遮罩不再切换最小化，卡片区域可交互', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)

    const mask = document.querySelector('.absolute.inset-0') as HTMLElement
    expect(mask).toBeInTheDocument()
    // 遮罩不拦截指针：底部条带内遮罩之下的其他 UI 保持可点击
    expect(mask.className).toContain('pointer-events-none')
    expect(mask.className).not.toContain('pointer-events-auto')

    // 点击遮罩区域不再触发最小化（待审批不阻塞用户其他操作）
    fireEvent.click(mask)
    expect(useInteractionStore.getState().isMinimized).toBe(false)

    // 卡片容器保持可交互
    const cardContainer = card().closest('.pointer-events-auto')
    expect(cardContainer).not.toBeNull()
  })
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
  it('卡片宽度容器在视口内自适应加宽：max-w 上限 + w-full 小视口收缩 + mx-4 视口边距', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)
    // 宽度上限容器：宽度 = min(上限, 视口-32px)，长选项卡片在宽视口下更宽
    const widthBox = card().closest('.max-w-4xl')
    expect(widthBox).not.toBeNull()
    expect(widthBox!.className).toMatch(/w-full/)
    expect(widthBox!.className).toMatch(/mx-4/)
  })
})
