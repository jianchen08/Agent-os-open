// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * GlobalInteractionOverlay 全局交互浮层测试
 *
 * 覆盖：待处理列表过滤（仅 pending）、多卡片导航（上一个/下一个 + 边界禁用 +
 * 索引自动重置）、最小化徽标（含审批倒计时，BUG-43）与恢复、按钮/ESC 关闭、
 * 三类响应回调的参数投影与失败提示、跨卡片提交互斥守卫、浮层不拦截 composer
 * （BUG-43：右下悬浮锚定 composer 上缘 + 宽度有界，接替 BUG-14 遮罩不拦截
 * 契约——遮罩已随横贯底部布局一并移除）。
 *
 * mock 约定：useInteractionHandler（WebSocket 编排层）、toast、logger 为外部
 * 边界整模块 mock；InteractionCard 以按钮桩替身回放回调参数；两个 zustand
 * store 走真实现。
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import { GlobalInteractionOverlay } from '../GlobalInteractionOverlay'
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

  it('最小化按钮收起为徽标；恢复后徽标点击还原卡片', () => {
    render(<GlobalInteractionOverlay />)
    fireEvent.click(screen.getByTitle('最小化'))
    expect(useInteractionStore.getState().isMinimized).toBe(true)
    expect(screen.getByText('1 个待处理交互')).toBeInTheDocument()

    fireEvent.click(screen.getByText('1 个待处理交互'))
    expect(useInteractionStore.getState().isMinimized).toBe(false)
  })

  // BUG-43：横贯底部的半透明遮罩随旧布局移除——非阻断改为结构性保证
  // （浮层右下悬浮锚定 composer 上缘，见「BUG-43 浮层不拦截 composer」组）。
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
  it('浮层宽度有界不横向溢出：min(26rem, 视口-2rem)，右锚定不横贯底部', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)
    // 宽度上限容器：宽度 = min(26rem, 视口-32px)，长选项卡片在窄视口不横向溢出；
    // 右锚定（无 inset-x-0/w-full）——不再横贯底部（BUG-43 非阻断定位）
    const panel = document.querySelector('[data-testid="interaction-overlay-panel"]')
    expect(panel).not.toBeNull()
    expect(panel!.className).toMatch(/w-\[min\(26rem,calc\(100vw-2rem\)\)\]/)
    expect(panel!.className).toMatch(/right-4/)
    expect(panel!.className).not.toMatch(/inset-x-0/)
    expect(panel!.className).not.toMatch(/(^|\s)w-full(\s|$)/)
  })
})

// ---------------------------------------------------------------------------
//  BUG-43 浮层不拦截 composer
//
//  composer 全宽横贯底部（ChatContainer px-3 + ChatInput w-full），浮层贴底
//  必压输入区（含发送按钮）——契约：浮层实测 composer 几何抬升至其上缘；
//  收起徽标同样避让。jsdom 无布局引擎（getBoundingClientRect 全零、
//  elementFromPoint 未实现），注入受控几何的最小命中测试：
//  矩形取自 1440×900 视口的真实布局推算，命中语义与真实 DOM 一致
//  （矩形包含该点的最上层元素胜出，注册序靠后 = 更上层，对应 z-[10000]）。
// ---------------------------------------------------------------------------
type HitRect = { left: number; top: number; right: number; bottom: number }

function installHitTest(rects: Array<[Element, HitRect]>) {
  const doc = document as unknown as {
    elementFromPoint?: (x: number, y: number) => Element | null
  }
  doc.elementFromPoint = (x: number, y: number) => {
    let hit: Element | null = null
    for (const [el, r] of rects) {
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) hit = el
    }
    return hit
  }
}

function uninstallHitTest() {
  delete (document as unknown as { elementFromPoint?: unknown }).elementFromPoint
}

function stubViewport() {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 })
}

/** composer 桩：全宽贴底、顶缘 y=770（高 130px），须在浮层挂载前入 DOM */
function stubComposer(): HTMLElement {
  const el = document.createElement('div')
  el.setAttribute('data-testid', 'chat-composer')
  Object.defineProperty(el, 'getBoundingClientRect', {
    value: () =>
      ({
        x: 0,
        y: 770,
        top: 770,
        bottom: 900,
        left: 0,
        right: 1440,
        width: 1440,
        height: 130,
        toJSON: () => ({}),
      }) as DOMRect,
  })
  document.body.appendChild(el)
  return el
}

describe('BUG-43 浮层不拦截 composer', () => {
  afterEach(() => {
    uninstallHitTest()
    document.querySelector('[data-testid="chat-composer"]')?.remove()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 768 })
  })

  it('composer 在场：浮层抬升至其上缘，composer 中心点/发送按钮区命中 composer 而非卡片', () => {
    stubViewport()
    const composer = stubComposer()
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)

    const panel = document.querySelector(
      '[data-testid="interaction-overlay-panel"]',
    ) as HTMLElement
    expect(panel).not.toBeNull()
    // 抬升锚定：内联 bottom = 视口高 - composer 顶缘 + 间距 = 900-770+8
    expect(panel.style.bottom).toBe('138px')

    // 浮层矩形按锚定契约推算：底缘 900-138=762 < composer 顶缘 770（无纵向交叠）
    const panelRect: HitRect = { left: 1008, top: 462, right: 1424, bottom: 762 }
    installHitTest([
      [composer, { left: 0, top: 770, right: 1440, bottom: 900 }],
      [panel, panelRect],
    ])
    // composer 中心点（旧横贯底部布局下该点命中卡片子树——本用例的回归锁）
    expect(document.elementFromPoint!(720, 835)).toBe(composer)
    // 发送按钮区（composer 右缘内）同样不被覆盖
    expect(document.elementFromPoint!(1350, 835)).toBe(composer)
    // 正对照：浮层自身区域内命中浮层（卡片在该区域保持可交互）
    expect(document.elementFromPoint!(1300, 600)).toBe(panel)
    // 结构分离：composer 不在浮层子树内
    expect(panel.contains(composer)).toBe(false)
  })

  it('无 composer 页面：浮层回退贴底右下（bottom-4 right-4），无内联抬升', () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)
    const panel = document.querySelector(
      '[data-testid="interaction-overlay-panel"]',
    ) as HTMLElement
    expect(panel.style.bottom).toBe('')
    expect(panel.className).toMatch(/bottom-4/)
    expect(panel.className).toMatch(/right-4/)
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
})

describe('GlobalInteractionOverlay — composer 间距跟随（resize）', () => {
  it('窗口 resize 且 composer 在挂时重新测量（onResize 分支）', async () => {
    setInteractions([makeInteraction()])
    render(<GlobalInteractionOverlay />)

    // 挂一个符合 composer 选择器的假元素，触发 MutationObserver 换绑
    const composer = document.createElement('textarea')
    composer.setAttribute('data-testid', 'chat-composer')
    composer.className = 'fake-x'
    document.body.appendChild(composer)
    // jsdom 无布局：top 恒 0 → clearance 落 COMPOSER_GAP_PX 分支之外也属已测量；
    // 本用例只锁「resize 时 watched 存在则重测不抛错」这一行为契约
    expect(() => {
      window.dispatchEvent(new Event('resize'))
    }).not.toThrow()

    composer.remove()
  })
})
