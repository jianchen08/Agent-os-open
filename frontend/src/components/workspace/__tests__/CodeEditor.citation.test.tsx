// @feature FP-0.2.评审引用交互 R5：输入框引用卡片化 | @ci: frontend-test
/**
 * CodeEditor 引用/评审入口测试（R5 落点卡片化改写：结构化条目入列）。
 *
 * 行为契约（卡片只是输入面显示形态，发送产物不变）：
 *  - 「引用」→ CitationItem 入列（kind 按文件扩展名判定，title=文件路径信息，
 *    excerpt=行号定格快照），不再 requestInsert 引用文本；
 *  - 「评审」（可带意见）→ 条目带 comment（发送装配期合成块引用+意见，组装
 *    真源=composeCitationEntry 与消息流同源）；
 *  - 意见为空 → 「评审」等价无评语卡片。
 */

import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useSessionStore } from '@/stores/sessionStore'
import { mockSelection } from '@/test/fakeSelection'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { CodeEditor } from '../CodeEditor'

vi.mock('react-syntax-highlighter/dist/esm/prism-light', () => {
  const MockHighlighter = ({
    children,
    language,
  }: {
    children?: React.ReactNode
    language?: string
  }) => (
    <pre data-testid="highlighter" data-language={language}>{children}</pre>
  )
  ;(MockHighlighter as unknown as Record<string, unknown>).registerLanguage = () => {}
  ;(MockHighlighter as unknown as Record<string, unknown>).alias = () => {}
  return { default: MockHighlighter }
})
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

const pyFile = { filePath: 'src/main.py', content: 'line1\nline2\nline3\n' }

/** draftKey（CodeEditor 与 ChatContainer 同构：activeTabId || activeSessionId） */
const DRAFT_KEY = 'session-1'
const citationsOf = () => useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY] ?? []

/** 代码预览分支的内容容器（highlighter mock 的父 div） */
function codePreviewContainer(): HTMLElement {
  return screen.getByTestId('highlighter').parentElement as HTMLElement
}

/** 触发预览区选中 → 浮动按钮出现 */
function selectInPreview(needle: string) {
  mockSelection(codePreviewContainer(), needle)
  fireEvent.mouseUp(codePreviewContainer())
}

beforeEach(() => {
  localStorage.clear()
  useSessionStore.setState({ activeSessionId: DRAFT_KEY })
  useAgentTabStore.setState({ activeTabId: null, tabs: [] })
  useChatInputStore.setState({ pendingInsert: null, drafts: {}, citationsByDraftKey: {} })
  render(<CodeEditor {...pyFile} onSave={vi.fn().mockResolvedValue(true)} />)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('CodeEditor 引用/评审入口（R5 卡片化）', () => {
  it('选中 → 意见输入 + 「评审」→ 条目入列（kind=code，title/excerpt/comment 齐全）', () => {
    selectInPreview('line2')
    fireEvent.change(screen.getByTestId('floating-quote-comment'), {
      target: { value: '这段实现要复核' },
    })
    fireEvent.click(screen.getByTestId('floating-quote-review'))

    // 来源=文件路径信息（单行 L2），excerpt=行号定格快照
    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({
      kind: 'code',
      title: 'src/main.py:L2',
      excerpt: 'L2: line2',
      comment: '这段实现要复核',
    })
    expect(useChatInputStore.getState().pendingInsert).toBeNull()
  })

  it('意见为空 → 「评审」等价无评语卡片（发送装配期等价纯引用块）', () => {
    selectInPreview('line2')
    fireEvent.click(screen.getByTestId('floating-quote-review'))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ kind: 'code', title: 'src/main.py:L2', excerpt: 'L2: line2' })
    expect(list[0].comment).toBeUndefined()
  })

  it('「引用」→ 无评语卡片入列（结构化条目，旧直接引用文本格式退役）', () => {
    selectInPreview('line2')
    fireEvent.click(screen.getByRole('button', { name: /引用/ }))
    expect(citationsOf()).toHaveLength(1)
    expect(citationsOf()[0]).toMatchObject({
      kind: 'code',
      title: 'src/main.py:L2',
      excerpt: 'L2: line2',
    })
    expect(citationsOf()[0].comment).toBeUndefined()
    expect(useChatInputStore.getState().pendingInsert).toBeNull()
  })

  it('跨行选中评审：excerpt 逐行带行号', () => {
    // mock highlighter 子内容为单文本节点，跨行 needle 可直接定位（同既有跨行用例）
    mockSelection(codePreviewContainer(), 'line1\nline2')
    fireEvent.mouseUp(codePreviewContainer())
    fireEvent.click(screen.getByTestId('floating-quote-review'))

    const list = citationsOf()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({
      kind: 'code',
      title: 'src/main.py:L1-2',
      excerpt: 'L1: line1\nL2: line2',
    })
  })

  it('评语输入框 auto-grow（R4 件①接线）：输入后 style.height 封顶 max-h-24（96px）', () => {
    selectInPreview('line2')
    const el = screen.getByTestId('floating-quote-comment') as HTMLTextAreaElement
    // jsdom 无布局：桩定 scrollHeight 模拟本征内容高（断言换算行为非布局）
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 120 })
    fireEvent.change(el, { target: { value: '评语' } })
    expect(el.style.height).toBe('96px')
  })

  it('输入框统一语义（R7 Bug①）：浮条活跃期 mousedown 落输入框容器 → preventDefault 保选区，浮条不消失', () => {
    // 文件页签侧同刀统一：浮条消费方是 CodeEditor（absolute 定位实例），
    // 保护区行为在共通 CitationFloatBar 内（消息流侧同源断言见 MessageItem.citationFloat）
    const probe = document.createElement('div')
    probe.setAttribute('data-testid', 'chat-input-box')
    const probeTextarea = document.createElement('textarea')
    probeTextarea.setAttribute('data-testid', 'chat-input-textarea')
    probe.appendChild(probeTextarea)
    document.body.appendChild(probe)

    try {
      selectInPreview('line2')
      expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()

      const evt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
      probeTextarea.dispatchEvent(evt)

      expect(evt.defaultPrevented).toBe(true)
      expect(screen.getByTestId('citation-float-bar')).toBeInTheDocument()
    } finally {
      probe.remove()
    }
  })
})
