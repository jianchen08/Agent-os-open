// @feature FP-0.2.四 前端Schema @ci frontend-test
/**
 * MessageItem 覆盖缺口补充测试（与既有 MessageItem*.test 互补，不重复）：
 * - MessageEditor 编辑流：挂载聚焦/改动自适应、Ctrl+Enter 保存（有改动才 onSave）、
 *   未改动提交=取消、Esc 取消、取消按钮、保存按钮空值禁用
 * - 复制链路：clipboard 成功写入（版本内容优先于原文）；clipboard 拒绝 → reportError 上报
 * - 版本内容（onContentUpdate）替代显示
 * - AI 平铺模式（bubbleAiMode=flat）：无背景图=透明裸排；背景图激活=半透明气泡面+模糊
 * - 用户附件：图片 → ImageGallery；文件三形态图标分型（代码/文档/通用）；
 *   点击有 url 附件调 openAttachment、无 url 静默；空内容且无附件 → 不渲染气泡
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import { useThemeStore } from '@/stores/themeStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { Message } from '@/types/models'

vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="user-markdown">{content}</div>
  ),
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: () => ({ activeSessionId: 'session-1' }),
}))
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions: [] }),
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
}))
vi.mock('@/services/attachmentOpener', () => ({ openAttachment: vi.fn() }))
vi.mock('@/components/media/ImageGallery', () => ({
  ImageGallery: ({ images }: { images: { id: string; title: string }[] }) => (
    <div data-testid="gallery">{images.map((i) => <span key={i.id}>{i.title}</span>)}</div>
  ),
}))
vi.mock('@/components/chat/MessageContentRenderer', () => ({ default: () => null }))

/** MessageActions 测试替身：全部动作 prop 暴露为按钮（真实组件交互归其自身测试） */
vi.mock('@/components/chat/MessageActions', () => ({
  MessageActions: (props: Record<string, ((...a: unknown[]) => void) | undefined>) => (
    <div data-testid="actions">
      <button onClick={() => void props.onCopy?.()}>A-copy</button>
      <button onClick={() => props.onEdit?.()}>A-edit</button>
      <button onClick={() => props.onContentUpdate?.('v2-content')}>A-update</button>
      <button onClick={() => props.onRegenerate?.()}>A-regen</button>
      <button onClick={() => props.onRollbackTo?.('m-1')}>A-rollback</button>
    </div>
  ),
}))

/** useMessageRender 桩：保留 versionContent 替代 displayContent 的真实合成规则 */
vi.mock('@/components/chat/hooks/useMessageRender', () => ({
  default: (args: { versionContent: string | null; message: { content: string } }) => ({
    fragments: [],
    displayContent: args.versionContent ?? args.message.content,
  }),
}))

import { openAttachment } from '@/services/attachmentOpener'
import { reportError } from '@/services/errorReporting'

function makeMessage(partial: Partial<Message>): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'user',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...partial,
  } as Message
}

const themePrev = useThemeStore.getState()
const clipboardState: { writeText?: ReturnType<typeof vi.fn> } = {}

function setClipboard(writeText: ReturnType<typeof vi.fn> | undefined) {
  clipboardState.writeText = writeText
  Object.defineProperty(window.navigator, 'clipboard', {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  })
}

afterEach(() => {
  vi.clearAllMocks()
  useThemeStore.setState({ bubbleAiMode: themePrev.bubbleAiMode, bgImageActive: themePrev.bgImageActive })
  setClipboard(undefined)
})

/** 打开编辑器并取 textarea */
async function openEditor() {
  fireEvent.click(screen.getByText('A-edit'))
  return await screen.findByRole('textbox')
}

describe('MessageItem 编辑器（MessageEditor）', () => {
  it('挂载即聚焦并把光标移到末尾', async () => {
    const { container } = renderWithProviders(
      <MessageItem message={makeMessage({ content: '原文' })} />,
    )
    const textarea = await openEditor()
    expect(container.ownerDocument.activeElement).toBe(textarea)
    expect(textarea.selectionStart).toBe('原文'.length)
  })

  it('Ctrl+Enter：有改动才保存（onEdit 收新值），未改动提交=取消', async () => {
    const onEdit = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<MessageItem message={makeMessage({ content: '原文' })} onEdit={onEdit} />)

    const textarea = await openEditor()
    fireEvent.change(textarea, { target: { value: '改过的内容' } })
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true })
    await screen.findByText('A-edit') // 编辑器关闭回到常态
    expect(onEdit).toHaveBeenCalledWith('m-1', '改过的内容')

    // 未改动提交 → 走取消（onEdit 不再被调）
    fireEvent.click(screen.getByText('A-edit'))
    const textarea2 = screen.getByRole('textbox')
    fireEvent.keyDown(textarea2, { key: 'Enter', ctrlKey: true })
    expect(onEdit).toHaveBeenCalledTimes(1)
    expect(screen.getByText('A-edit')).toBeInTheDocument()
  })

  it('Esc 取消与取消按钮都关闭编辑器且不落保存', async () => {
    const onEdit = vi.fn()
    renderWithProviders(<MessageItem message={makeMessage({ content: '原文' })} onEdit={onEdit} />)

    const textarea = await openEditor()
    fireEvent.keyDown(textarea, { key: 'Escape' })
    expect(screen.queryByRole('textbox')).toBeNull()

    fireEvent.click(screen.getByText('A-edit'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('保存按钮空值禁用、非空可提交', async () => {
    const onEdit = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<MessageItem message={makeMessage({ content: '' })} onEdit={onEdit} />)
    await openEditor()

    const save = screen.getByRole('button', { name: '保存' })
    expect(save).toBeDisabled()

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '新内容' } })
    expect(save).toBeEnabled()
    fireEvent.click(save)
    await screen.findByText('A-edit')
    expect(onEdit).toHaveBeenCalledWith('m-1', '新内容')
  })
})

describe('MessageItem 复制与版本内容', () => {
  it('复制写入版本内容（优先于原文）；拒绝时 reportError 上报', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    setClipboard(writeText)
    renderWithProviders(<MessageItem message={makeMessage({ content: '原文' })} />)

    // 版本内容更新后，复制的是版本内容而非原文
    fireEvent.click(screen.getByText('A-update'))
    expect(screen.getByTestId('user-markdown').textContent).toBe('v2-content')
    fireEvent.click(screen.getByText('A-copy'))
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('v2-content'))
    expect(reportError).not.toHaveBeenCalled()

    // clipboard 拒绝 → reportError（CLIENT/MessageItem/copyToClipboard）
    setClipboard(vi.fn().mockRejectedValue(new Error('denied')))
    fireEvent.click(screen.getByText('A-copy'))
    await vi.waitFor(() =>
      expect(reportError).toHaveBeenCalledWith(
        'denied',
        expect.objectContaining({ componentName: 'MessageItem', operation: 'copyToClipboard' }),
      ),
    )
  })

  it('onRegenerate / onRollbackTo 透传不崩', () => {
    const onRegenerate = vi.fn()
    const onRollbackTo = vi.fn()
    renderWithProviders(
      <MessageItem
        message={makeMessage({ content: '原文' })}
        onRegenerate={onRegenerate}
        onRollbackTo={onRollbackTo}
      />,
    )
    fireEvent.click(screen.getByText('A-regen'))
    fireEvent.click(screen.getByText('A-rollback'))
    expect(onRegenerate).toHaveBeenCalledTimes(1)
    expect(onRollbackTo).toHaveBeenCalledWith('m-1')
  })
})

describe('MessageItem AI 平铺模式（主题声明）', () => {
  function assistantBubble(): HTMLElement {
    // 从消息文本向上找气泡盒（Avatar 也带 overflow-hidden 且在 DOM 前面，不能按类名直查）
    return screen.getByText('AI 回答').closest('.overflow-hidden') as HTMLElement
  }

  it('flat + 无背景图：透明裸排（无圆角/阴影/边框）', () => {
    useThemeStore.setState({ bubbleAiMode: 'flat', bgImageActive: false })
    renderWithProviders(
      <MessageItem message={makeMessage({ role: 'assistant', content: 'AI 回答' })} />,
    )
    // 断言内联 style 属性原文：jsdom cssstyle 对 background/border-radius 简写
    // 赋值会拒收（读回恒空），style attr 才是 React 实际写入了什么
    const style = assistantBubble().getAttribute('style') ?? ''
    expect(style).toContain('background: transparent')
    expect(style).not.toContain('border-radius')
    expect(style).not.toContain('box-shadow')
    expect(style).toContain('padding: 0.35rem 0.5rem')
  })

  it('flat + 背景图激活：半透明气泡面 + 模糊 + 不对称圆角（文字不裸贴背景）', () => {
    useThemeStore.setState({ bubbleAiMode: 'flat', bgImageActive: true })
    renderWithProviders(
      <MessageItem message={makeMessage({ role: 'assistant', content: 'AI 回答' })} />,
    )
    const style = assistantBubble().getAttribute('style') ?? ''
    expect(style).toContain('background: var(--bubble-ai-bg')
    expect(style).toContain('border-radius: 18px 18px 18px 6px')
    expect(style).toContain('backdrop-filter: blur(6px)')
    expect(style).toContain('padding: 0.5rem 0.75rem')
  })
})

describe('MessageItem 用户附件', () => {
  it('图片附件 → ImageGallery；文件附件按 mime 三形态选图标并显示文件名', () => {
    renderWithProviders(
      <MessageItem
        message={makeMessage({
          content: '看附件',
          attachments: [
            { id: 'a1', name: 'cat.png', url: '/uploads/cat.png', type: 'image/png' },
            { id: 'a2', name: 'run.py', url: '/uploads/run.py', type: 'text/x-python' },
            { id: 'a3', name: 'doc.pdf', url: '/uploads/doc.pdf', mime_type: 'application/pdf' },
            { id: 'a4', name: 'bin.dat', url: '/uploads/bin.dat', type: 'application/octet-stream' },
          ],
        } as Partial<Message>)}
      />,
    )
    expect(screen.getByTestId('gallery')).toHaveTextContent('cat.png')
    // 三个文件附件按钮齐全（代码/文档/通用三分支都走到）
    expect(screen.getByText('run.py')).toBeInTheDocument()
    expect(screen.getByText('doc.pdf')).toBeInTheDocument()
    expect(screen.getByText('bin.dat')).toBeInTheDocument()
  })

  it('点击有 url 的附件调 openAttachment；无 url 静默不调', () => {
    vi.mocked(openAttachment).mockClear()
    renderWithProviders(
      <MessageItem
        message={makeMessage({
          content: '看附件',
          attachments: [
            { id: 'a2', name: 'run.py', url: '/uploads/run.py', type: 'text/x-python' },
            { id: 'a5', name: 'nourl.txt', type: 'text/plain' },
          ],
        } as Partial<Message>)}
      />,
    )
    fireEvent.click(screen.getByText('run.py'))
    expect(openAttachment).toHaveBeenCalledWith({ id: 'a2', name: 'run.py', url: '/uploads/run.py' })

    openAttachment.mockClear()
    fireEvent.click(screen.getByText('nourl.txt'))
    expect(openAttachment).not.toHaveBeenCalled()
  })

  it('空内容且无任何附件 → 不渲染气泡', () => {
    renderWithProviders(<MessageItem message={makeMessage({ content: '' })} />)
    expect(screen.queryByTestId('user-markdown')).toBeNull()
    expect(screen.queryByTestId('gallery')).toBeNull()
  })
})
