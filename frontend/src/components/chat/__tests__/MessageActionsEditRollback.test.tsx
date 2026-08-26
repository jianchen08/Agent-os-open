/**
 * 消息操作交互测试（批次 E：回退二次确认 + 编辑入口）。
 *
 * 行为契约（docs/working/聊天中断保留与重新生成回退方案_20260826.md §二.4/5）：
 *  - 「回退」：user 消息 hover 操作区的 rotate-ccw 按钮，点击展开内联二次确认；
 *    确认后触发 onRollbackTo(messageId)，取消不触发且收起确认条；
 *  - 「编辑」：user 消息的编辑按钮，点击触发 onEdit（内联 MessageEditor 由
 *    MessageItem 接管，本组件只负责入口）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MessageActions } from '../MessageActions'
import type { Message } from '@/types/models'

function makeUserMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'user',
    content: '原始问题',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

describe('MessageActions 回退/编辑交互（批次 E）', () => {
  it('user 消息回退：点击展开二次确认，确认后触发 onRollbackTo(messageId)', () => {
    const onRollbackTo = vi.fn()
    render(
      <MessageActions
        message={makeUserMessage()}
        sessionId="session-1"
        onRollbackTo={onRollbackTo}
      />,
    )
    const rollbackBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '回退到这条消息')
    expect(rollbackBtn).toBeDefined()
    fireEvent.click(rollbackBtn!)
    // 二次确认条出现
    expect(screen.getByTestId('rollback-confirm')).toBeDefined()
    fireEvent.click(screen.getByText('确认'))
    expect(onRollbackTo).toHaveBeenCalledWith('msg-1')
  })

  it('user 消息回退：二次确认后点取消不触发回调且确认条收起', () => {
    const onRollbackTo = vi.fn()
    render(
      <MessageActions
        message={makeUserMessage()}
        sessionId="session-1"
        onRollbackTo={onRollbackTo}
      />,
    )
    const rollbackBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '回退到这条消息')
    fireEvent.click(rollbackBtn!)
    fireEvent.click(screen.getByText('取消'))
    expect(onRollbackTo).not.toHaveBeenCalled()
    expect(screen.queryByTestId('rollback-confirm')).toBeNull()
  })

  it('未确认时（仅展开后不操作）不触发回调', () => {
    const onRollbackTo = vi.fn()
    render(
      <MessageActions
        message={makeUserMessage()}
        sessionId="session-1"
        onRollbackTo={onRollbackTo}
      />,
    )
    const rollbackBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '回退到这条消息')
    fireEvent.click(rollbackBtn!)
    expect(onRollbackTo).not.toHaveBeenCalled()
  })

  it('user 消息编辑：点击触发 onEdit（打开内联编辑器由 MessageItem 接管）', () => {
    const onEdit = vi.fn()
    render(
      <MessageActions
        message={makeUserMessage()}
        sessionId="session-1"
        onEdit={onEdit}
      />,
    )
    const editBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '编辑并重新发送')
    expect(editBtn).toBeDefined()
    fireEvent.click(editBtn!)
    expect(onEdit).toHaveBeenCalledTimes(1)
  })

  it('user 消息未传回调时不显示编辑/回退按钮', () => {
    render(<MessageActions message={makeUserMessage()} sessionId="session-1" />)
    const editBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '编辑并重新发送')
    const rollbackBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '回退到这条消息')
    expect(editBtn).toBeUndefined()
    expect(rollbackBtn).toBeUndefined()
  })

  it('assistant 消息不显示回退/编辑按钮（回退目标只能是 user 消息）', () => {
    const onRollbackTo = vi.fn()
    const onEdit = vi.fn()
    render(
      <MessageActions
        message={{ ...makeUserMessage(), role: 'assistant' } as Message}
        sessionId="session-1"
        onRollbackTo={onRollbackTo}
        onEdit={onEdit}
      />,
    )
    const editBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '编辑并重新发送')
    const rollbackBtn = screen
      .getAllByRole('button')
      .find((b) => b.getAttribute('title') === '回退到这条消息')
    expect(editBtn).toBeUndefined()
    expect(rollbackBtn).toBeUndefined()
  })
})
