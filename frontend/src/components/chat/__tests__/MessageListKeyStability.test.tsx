/** @feature: FP-0.2.流式气泡稳定 消息气泡 key 稳定性 | @ci: frontend-test */
/**
 * 权威 sequence 落地不得重挂气泡（2026-10-17 用户实证：思考/生成中气泡跳一下）。
 *
 * 流式占位创建时 sequence 挂空（stream_start 不携带消息 seq），new_message/
 * stream_end 的权威 seq 随后经 updateMessage 落地。修复前 React key 为
 * `${id}-${sequence ?? index}`：seq 落地即 key 变化 → 整个气泡卸载重挂，
 * 内容闪一下（「跳一下」）。修复后 key 只用消息 id（store 按 id 键控，唯一），
 * seq 落地仅更新数据不换 DOM 节点。
 */
import { render, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { MessageList } from '../MessageList'
import type { Message } from '@/types/models'

vi.mock('../MessageItem', () => ({
  MessageItem: ({ message }: { message: Message }) => (
    <div data-testid={`message-item-${message.id}`}>{message.content}</div>
  ),
}))

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-key-stability-001',
    sessionId: 'sess-key-stability',
    role: 'assistant',
    content: '已完成正文',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...overrides,
  }
}

const baseProps = {
  className: '',
  isGenerating: false,
}

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(0)
    return 0
  })
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('MessageList key 稳定性：权威 sequence 落地不重挂气泡', () => {
  it('sequence 从挂空到权威值：DOM 节点保持同一（不卸载重挂）', () => {
    const { container, rerender } = render(
      <MessageList {...baseProps} messages={[makeMessage({ sequence: undefined })]} />,
    )
    const before = container.querySelector('[data-msg-id="msg-key-stability-001"]')
    expect(before).not.toBeNull()

    rerender(
      <MessageList {...baseProps} messages={[makeMessage({ sequence: 5 })]} />,
    )

    const after = container.querySelector('[data-msg-id="msg-key-stability-001"]')
    expect(after).toBe(before)
  })

  it('新消息追加导致既有消息 index 变化：既有气泡节点不重建', () => {
    const { container, rerender } = render(
      <MessageList
        {...baseProps}
        messages={[makeMessage({ id: 'msg-a', sequence: undefined })]}
      />,
    )
    const before = container.querySelector('[data-msg-id="msg-a"]')
    expect(before).not.toBeNull()

    rerender(
      <MessageList
        {...baseProps}
        messages={[
          makeMessage({ id: 'msg-a', sequence: 1 }),
          makeMessage({ id: 'msg-b', role: 'user', content: '新消息', sequence: 2 }),
        ]}
      />,
    )

    expect(container.querySelector('[data-msg-id="msg-a"]')).toBe(before)
  })
})
