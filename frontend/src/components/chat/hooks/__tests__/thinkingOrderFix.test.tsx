/**
 * Bug 1 回归测试：思考内容显示顺序颠倒
 *
 * buildFragmentsFromParts 应确保 thinking 片段始终在 text 片段之前渲染，
 * 即使 parts 数组中 text 在 thinking 之前（流式事件竞态场景）。
 *
 * 本测试直接调用生产函数 buildFragmentsFromParts，构成真正的回归保护。
 */
import { describe, it, expect } from 'vitest'
import { buildFragmentsFromParts } from '@/components/chat/hooks/useMessageRender'
import type { Message } from '@/types/models'
import type { MessagePart } from '@/types/messageParts'

const BASE_MSG: Message = {
  id: 'msg-test-001',
  sessionId: 'session-test',
  role: 'assistant',
  content: '',
  timestamp: new Date().toISOString(),
  parentId: null,
  status: 'completed',
}

function makeMessage(parts: MessagePart[]): Message {
  return { ...BASE_MSG, parts }
}

describe('Bug 1: 思考内容应在正式文本之前渲染', () => {
  it('parts 数组 [text, thinking] → 渲染顺序应为 [thinking, text]', () => {
    // 模拟流式竞态：text part 先于 thinking part 进入数组
    const msg = makeMessage([
      { type: 'text', content: '正式回复', state: 'done' },
      { type: 'thinking', content: '我在思考...', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    // 断言：thinking 在 text 之前
    const thinkIdx = fragments.findIndex((f) => f.type === 'thinking')
    const textIdx = fragments.findIndex((f) => f.type === 'text')
    expect(thinkIdx).toBeGreaterThanOrEqual(0)
    expect(textIdx).toBeGreaterThanOrEqual(0)
    expect(thinkIdx).toBeLessThan(textIdx)
  })

  it('parts 数组 [thinking, text] → 顺序不变', () => {
    const msg = makeMessage([
      { type: 'thinking', content: '我在思考...', state: 'done' },
      { type: 'text', content: '正式回复', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    expect(fragments[0].type).toBe('thinking')
    expect(fragments[1].type).toBe('text')
  })

  it('parts 数组含 tool_call + thinking + text → thinking 仍在最前', () => {
    const msg = makeMessage([
      { type: 'text', content: '回复', state: 'done' },
      { type: 'tool_call', callId: 'c1', name: 'tool1', args: {}, state: 'done' },
      { type: 'thinking', content: '思考', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    expect(fragments[0].type).toBe('thinking')
    // tool_call 和 text 保持原始相对顺序（在 thinking 之后）
    const textIdx = fragments.findIndex((f) => f.type === 'text')
    const toolIdx = fragments.findIndex((f) => f.type === 'tool_call')
    expect(textIdx).toBeLessThan(toolIdx)
  })

  it('parts 数组无 thinking → 顺序不变', () => {
    const msg = makeMessage([
      { type: 'text', content: '回复1', state: 'done' },
      { type: 'text', content: '回复2', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    expect(fragments).toHaveLength(2)
    expect(fragments[0].type).toBe('text')
    expect(fragments[1].type).toBe('text')
  })
})
