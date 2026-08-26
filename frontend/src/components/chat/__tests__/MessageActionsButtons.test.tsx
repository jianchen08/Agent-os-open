/**
 * 消息操作按钮交互测试（批次 D：重新生成按钮）。
 *
 * 行为契约（docs/working/聊天中断保留与重新生成回退方案_20260826.md §二.3）：
 *  - 「重新生成」：最后一条 assistant 消息（含失败/中断态）上的按钮，点击触发 onRegenerate；
 *  - user 消息不显示重新生成；未传回调不显示按钮。
 * 批次 E（回退/编辑）交互见 MessageActionsEditRollback.test.tsx。
 */
import { fireEvent, render, screen } from '@testing-library/react'
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

describe('MessageActions 重新生成按钮', () => {
  it('assistant 消息显示重新生成按钮，点击触发 onRegenerate', () => {
    const onRegenerate = vi.fn()
    render(
      <MessageActions
        message={makeMessage('assistant')}
        sessionId="session-1"
        onRegenerate={onRegenerate}
      />,
    )
    const btn = screen.getByTestId('regenerate-button')
    expect(btn).toBeDefined()
    fireEvent.click(btn)
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })

  it('失败/中断态 assistant 消息同样显示重新生成按钮（失败态可重试）', () => {
    const onRegenerate = vi.fn()
    const { rerender } = render(
      <MessageActions
        message={makeMessage('assistant', { status: 'error' })}
        sessionId="session-1"
        onRegenerate={onRegenerate}
      />,
    )
    expect(screen.getByTestId('regenerate-button')).toBeDefined()

    rerender(
      <MessageActions
        message={makeMessage('assistant', { status: 'interrupted' })}
        sessionId="session-1"
        onRegenerate={onRegenerate}
      />,
    )
    expect(screen.getByTestId('regenerate-button')).toBeDefined()
  })

  it('未传 onRegenerate 回调时不显示重新生成按钮', () => {
    render(<MessageActions message={makeMessage('assistant')} sessionId="session-1" />)
    expect(screen.queryByTestId('regenerate-button')).toBeNull()
  })

  it('user 消息不显示重新生成按钮', () => {
    const onRegenerate = vi.fn()
    render(
      <MessageActions
        message={makeMessage('user')}
        sessionId="session-1"
        onRegenerate={onRegenerate}
      />,
    )
    expect(screen.queryByTestId('regenerate-button')).toBeNull()
  })

  it('禁用态：重新生成按钮不可点', () => {
    const onRegenerate = vi.fn()
    render(
      <MessageActions
        message={makeMessage('assistant')}
        sessionId="session-1"
        disabled
        onRegenerate={onRegenerate}
      />,
    )
    fireEvent.click(screen.getByTestId('regenerate-button'))
    expect(onRegenerate).not.toHaveBeenCalled()
  })
})
