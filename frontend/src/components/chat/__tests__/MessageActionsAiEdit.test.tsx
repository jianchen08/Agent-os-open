// @feature FP-T12 角色扮演成熟化 Wave B（AI 消息编辑入口反向锁定） @ci frontend-test
/**
 * AI 消息编辑入口反向锁定：内核无 assistant 消息内容编辑通道（regenerate 截断
 * 边界硬约束只认 role=user——工具配对完整性；无内容更新端点），复用用户编辑
 * 重发通道对 assistant 必然后端报错——入口按诚实原则撤下。待内核补 assistant
 * 内容编辑端点后随 ADR 恢复。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MessageActions } from '../MessageActions'
import type { Message } from '@/types/models'

function makeMessage(role: Message['role'], overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role,
    content: '内容',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

describe('MessageActions AI 消息编辑入口（诚实撤下）', () => {
  it('最后一条 assistant 即使有 onEdit 也不渲染编辑按钮', () => {
    const onEdit = vi.fn()
    render(
      <MessageActions
        message={makeMessage('assistant')}
        sessionId="session-1"
        isLastMessage
        onEdit={onEdit}
      />,
    )
    expect(screen.queryByTestId('edit-assistant-button')).toBeNull()
  })

  it('user 消息编辑入口不受影响（编辑并重新发送仍在）', () => {
    render(
      <MessageActions
        message={makeMessage('user')}
        sessionId="session-1"
        isUserMessage
        isLastMessage
        onEdit={vi.fn()}
      />,
    )
    expect(screen.getByTitle('编辑并重新发送')).toBeTruthy()
    expect(screen.queryByTestId('edit-assistant-button')).toBeNull()
  })
})
