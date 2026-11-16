// @feature FP-0.2.评审引用交互 R5：输入框引用卡片化 | @ci: frontend-test
/**
 * 消息流选中文字引用/评审浮层测试（R4 件③入口形态 + R5 落点卡片化改写）。
 *
 * 交互形态（用户裁定：消息流引用/评审入口与工作区制品同形态——选中消息文字
 * → 选区上方弹 CitationFloatBar）+ R5 落点契约（卡片入列，发送产物不变）：
 *  - 「引用」→ 结构化条目入列（kind=message，title=角色标签，excerpt=定格快照），
 *    不再 requestInsert 引用文本（旧通道退役，通道本体保留给队列退回等用途）；
 *  - 「评审」（可带意见）→ 条目带 comment；
 *  - excerpt 为入口截断快照（400 字+省略号，与发送装配真源同阈值——发送装配期
 *    经 composeCitationEntry 拼回的块引用与旧 requestInsert 通道输出等价）；
 *  - 无选区 → 无浮层；readOnly → 不挂选区浮层；
 *  - MessageActions 不再承载引用/评审三件（其余动作不动）。
 */

import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatInputStore } from '@/stores/chatInputStore'
import { mockSelection } from '@/test/fakeSelection'
import { renderWithProviders } from '@/test/renderWithProviders'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())
vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())
vi.mock('@/stores/agentStore', async () => (await import('./helpers/messageItemMocks')).agentStoreEmptyMock())
vi.mock('@/stores/interactionStore', async () => (await import('./helpers/messageItemMocks')).interactionStoreSelectorMock())
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/services/fileLoaderRegistry', async () => (await import('./helpers/messageItemMocks')).attachmentOpenerMock())

// assistant 默认气泡路径：渲染 fragments 文本（真实 useMessageRender 形态的桩）。
// 文本常量经 vi.hoisted——vi.mock 工厂提升后引用不落 TDZ。
const { ASSISTANT_TEXT } = vi.hoisted(() => ({ ASSISTANT_TEXT: '助手的回答内容' }))
vi.mock(
  '@/components/chat/hooks/useMessageRender',
  async () => (await import('./helpers/messageItemMocks')).useMessageRenderTextMock(ASSISTANT_TEXT),
)
vi.mock(
  '@/components/chat/MessageContentRenderer',
  async () => (await import('./helpers/messageItemMocks')).messageContentRendererTextMock(),
)

const TS = '2026-11-05T06:30:00Z'

function makeMessage(role: Message['role'], content: string): Message {
  return {
    id: role === 'user' ? 'user-1' : 'assistant-1',
    sessionId: 'session-1',
    sequence: 1,
    role,
    content,
    timestamp: TS,
    parentId: null,
    status: 'completed',
  } as Message
}

/** 气泡容器（选区挂点）：user/ai 由 data-bubble 标定 */
function bubbleContainer(role: 'user' | 'ai'): HTMLElement {
  const bubble = document.querySelector(`[data-bubble="${role}"]`)
  if (!bubble) throw new Error(`bubble not found: ${role}`)
  return bubble as HTMLElement
}

/** draftKey（MessageItem 与 ChatContainer 同构：activeTabId || activeSessionId；
 *  本套件 agentTabStore 走真实模块 activeTabId=null → draftKey=mock 会话） */
const DRAFT_KEY = 'session-1'

const citationsOf = () => useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY] ?? []

beforeEach(() => {
  useChatInputStore.setState({ pendingInsert: null, drafts: {}, citationsByDraftKey: {} })
})

describe('消息气泡选中文字 → 引用/评审浮层（用户气泡）', () => {
  it('选中 → 「引用」→ 结构化条目入列（kind=message，excerpt=快照），不再走 requestInsert', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', '请评审这一段')} />)

    mockSelection(bubbleContainer('user'), '评审这一段')
    fireEvent.mouseUp(bubbleContainer('user'))

    expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ kind: 'message', title: '用户', excerpt: '评审这一段' })
    // 旧通道退役：引用文本不再进 pendingInsert
    expect(useChatInputStore.getState().pendingInsert).toBeNull()
    // 动作后浮层收起
    expect(screen.queryByTestId('citation-float-bar')).not.toBeInTheDocument()
  })

  it('评语 + 「评审」→ 条目带 comment（发送装配期合成块引用+意见）', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', '请评审这一段')} />)
    mockSelection(bubbleContainer('user'), '评审这一段')
    fireEvent.mouseUp(bubbleContainer('user'))

    fireEvent.change(screen.getByTestId('floating-quote-comment'), {
      target: { value: '措辞再收敛些' },
    })
    fireEvent.click(screen.getByTestId('floating-quote-review'))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({
      kind: 'message',
      title: '用户',
      excerpt: '评审这一段',
      comment: '措辞再收敛些',
    })
    expect(useChatInputStore.getState().pendingInsert).toBeNull()
  })

  it('快照语义：浮层打开后选区塌陷，动作仍用弹出时的选区快照', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', '请评审这一段')} />)
    mockSelection(bubbleContainer('user'), '评审这一段')
    fireEvent.mouseUp(bubbleContainer('user'))
    expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()

    // 选区塌陷（模拟点击别处/输入框抢焦点后的选区状态）
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => '',
      rangeCount: 0,
      removeAllRanges: () => {},
    } as unknown as Selection)

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0].excerpt).toBe('评审这一段')
  })

  it('超长选区：excerpt 入口统一截断（400 字+省略号，与发送装配真源同阈值）', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', 'a'.repeat(500))} />)
    mockSelection(bubbleContainer('user'), 'a'.repeat(500))
    fireEvent.mouseUp(bubbleContainer('user'))

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    const item = citationsOf()[0]
    expect(item.excerpt).toContain('…')
    expect(item.excerpt).toContain('a'.repeat(400))
    expect(item.excerpt).not.toContain('a'.repeat(401))
  })

  it('折叠选区（无选区）→ 不弹浮层', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', '请评审这一段')} />)
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => '',
      rangeCount: 0,
      removeAllRanges: () => {},
    } as unknown as Selection)

    fireEvent.mouseUp(bubbleContainer('user'))

    expect(screen.queryByTestId('citation-float-bar')).not.toBeInTheDocument()
  })

  it('readOnly → 不挂选区浮层（与操作组入口同一可用性门）', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', '请评审这一段')} readOnly />)
    mockSelection(bubbleContainer('user'), '评审这一段')
    fireEvent.mouseUp(bubbleContainer('user'))

    expect(screen.queryByTestId('citation-float-bar')).not.toBeInTheDocument()
  })

  it('MessageActions 不再承载常驻引用/评审三件（复制等其余动作不动）', () => {
    renderWithProviders(<MessageItem message={makeMessage('user', '请评审这一段')} />)

    expect(screen.queryByTestId('citation-quote-button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('citation-comment-input')).not.toBeInTheDocument()
    expect(screen.queryByTestId('citation-review-button')).not.toBeInTheDocument()
    expect(screen.getByTitle('复制')).toBeInTheDocument()
  })
})

describe('消息气泡选中文字 → 引用浮层（助手气泡）', () => {
  it('助手气泡选中文字 → 「引用」→ 条目 title=助手、excerpt=快照', () => {
    renderWithProviders(<MessageItem message={makeMessage('assistant', ASSISTANT_TEXT)} />)

    mockSelection(bubbleContainer('ai'), ASSISTANT_TEXT)
    fireEvent.mouseUp(bubbleContainer('ai'))

    expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ kind: 'message', title: '助手', excerpt: ASSISTANT_TEXT })
  })
})

describe('输入框统一语义（R7 Bug①：浮条活跃期点输入框保选区保浮条）', () => {
  /** 消息 + 输入框容器桩同树（testid 契约真源在 ChatInput，此处桩等形） */
  function renderWithInputBox() {
    return renderWithProviders(
      <>
        <MessageItem message={makeMessage('user', '请评审这一段')} />
        <div data-testid="chat-input-box">
          <textarea data-testid="chat-input-textarea" />
        </div>
        <div data-testid="plain-area" />
      </>,
    )
  }

  function openBar() {
    mockSelection(bubbleContainer('user'), '评审这一段')
    fireEvent.mouseUp(bubbleContainer('user'))
    expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()
  }

  it('浮条活跃 → pointerdown 落输入框容器 → 浮条不关（高亮保持的前提）', () => {
    renderWithInputBox()
    openBar()

    fireEvent.pointerDown(screen.getByTestId('chat-input-textarea'))

    expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()
  })

  it('浮条活跃 → 先点输入框再点「引用」→ 照常入列（快照语义下动作不受影响）', () => {
    renderWithInputBox()
    openBar()

    fireEvent.pointerDown(screen.getByTestId('chat-input-textarea'))
    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ kind: 'message', title: '用户', excerpt: '评审这一段' })
    // 动作后浮层照常收起
    expect(screen.queryByTestId('citation-float-bar')).not.toBeInTheDocument()
  })

  it('浮条活跃 → pointerdown 落浮条外其他区域（非输入框）→ 仍关闭（既有语义不变）', () => {
    renderWithInputBox()
    openBar()

    fireEvent.pointerDown(screen.getByTestId('plain-area'))

    expect(screen.queryByTestId('citation-float-bar')).not.toBeInTheDocument()
  })
})
