// @feature FP-0.2.四 前端Schema @ci frontend-test
/**
 * MessageItem 剩余缺口补充测试（与既有 MessageItem*.test 互补）：
 * - pendingInteractions 非空时 selector 真实求值：assistant 流式消息 +
 *   同会话 pending 交互 → 「等待用户响应...」（区别于空 store 的「思考中...」）；
 * - 工具卡打开文件链：chat_card 声明 filePathSource → 卡片打开按钮 →
 *   MessageItem onOpenFile 包装 → 全局打开回调收到 (filePath, taskId)。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import { renderWithProviders } from '@/test/renderWithProviders'
import { loadChatCardDeclarations, clearChatCardDeclarations } from '@/utils/chatCardInterpreter'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { Message } from '@/types/models'

vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="user-markdown">{content}</div>
  ),
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: () => ({ activeSessionId: 'session-1' }),
}))

// 可变 store 桩：用例内改写 pendingInteractions（空数组 = selector 短路对照）
let pendingInteractions: Array<Record<string, unknown>> = []
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions }),
}))

vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
}))
vi.mock('@/services/attachmentOpener', () => ({ openAttachment: vi.fn() }))
vi.mock('@/components/chat/MessageContentRenderer', () => ({ default: () => null }))
vi.mock('@/components/chat/MessageActions', () => ({ MessageActions: () => null }))
vi.mock('@/components/chat/hooks/useMessageRender', () => {
  const useMessageRender = () => ({ fragments: [], isStreaming: false })
  return { useMessageRender, default: useMessageRender }
})

function makeMessage(partial: Partial<Message>): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...partial,
  } as Message
}

describe('MessageItem pending 交互等待态', () => {
  afterEach(() => {
    pendingInteractions = []
  })

  it('同会话 pending 交互：selector 求值且渲染「等待用户响应...」', () => {
    pendingInteractions = [{ threadId: 'session-1', status: 'pending' }]
    renderWithProviders(
      <MessageItem message={makeMessage({ role: 'assistant', status: 'streaming' })} />,
    )
    expect(screen.getByText('等待用户响应...')).toBeInTheDocument()
  })

  it('他线程 pending 交互：不算本消息等待，渲染「思考中...」', () => {
    pendingInteractions = [{ threadId: 'session-other', status: 'pending' }]
    renderWithProviders(
      <MessageItem message={makeMessage({ role: 'assistant', status: 'streaming' })} />,
    )
    expect(screen.queryByText('等待用户响应...')).not.toBeInTheDocument()
    expect(screen.getByText('思考中...')).toBeInTheDocument()
  })
})

describe('MessageItem 工具卡打开文件回调链', () => {
  const FILE_CARD_DECL = {
    icon: 'edit',
    title: '写入 {{args.path | basename | default:file_write}}',
    filePathSource: 'result.file',
  }

  beforeEach(() => {
    loadChatCardDeclarations([{ name: 'file_write', ui: { chat_card: FILE_CARD_DECL } }])
  })

  afterEach(() => {
    clearChatCardDeclarations()
  })

  it('点击卡片打开按钮：全局回调收到 (filePath, taskId)', () => {
    const onOpenFile = vi.fn()
    registerGlobalOpenFileCallback(onOpenFile)

    renderWithProviders(
      <MessageItem
        taskId="task-77"
        message={makeMessage({
          id: 'tool-9',
          role: 'tool',
          status: 'completed',
          toolName: 'file_write',
          toolResult: { file: 'D:/ws/report.md' },
        })}
      />,
    )

    const openBtn = screen.getByRole('button', { name: /打开文件 D:\/ws\/report\.md/ })
    fireEvent.click(openBtn)

    expect(onOpenFile).toHaveBeenCalledTimes(1)
    expect(onOpenFile).toHaveBeenCalledWith('D:/ws/report.md', 'task-77')
  })
})
