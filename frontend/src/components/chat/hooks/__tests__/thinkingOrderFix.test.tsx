/**
 * 思考内容渲染顺序回归测试
 *
 * buildFragmentsFromParts 严格按 parts 数组顺序渲染（= 接收顺序 = 最终态顺序），
 * 不再做"thinking 整体前置"重排。原因：多轮 LLM 调用的思考应与各自正文交错呈现
 * （思考1→正文1→思考2→正文2），把所有思考堆在一起会破坏交错语义。
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

describe('渲染顺序：严格按 parts 数组顺序，不做 thinking 前置重排', () => {
  it('parts 数组 [text, thinking] → 渲染顺序保持原序 [text, thinking]', () => {
    // 不再强制把 thinking 提到前面：数组顺序即渲染顺序
    const msg = makeMessage([
      { type: 'text', content: '正式回复', state: 'done' },
      { type: 'thinking', content: '我在思考...', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    // 断言：保持数组原始顺序
    expect(fragments[0].type).toBe('text')
    expect(fragments[1].type).toBe('thinking')
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

  it('parts 数组含 tool_call + thinking + text → 保持原始相对顺序', () => {
    // 多轮交错场景的关键保护：不把 thinking 提到最前
    const msg = makeMessage([
      { type: 'thinking', content: '第一轮思考', state: 'done' },
      { type: 'text', content: '回复', state: 'done' },
      { type: 'tool_call', callId: 'c1', name: 'tool1', args: {}, state: 'done' },
      { type: 'thinking', content: '第二轮思考', state: 'done' },
      { type: 'text', content: '最终回复', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    // 断言：严格按数组顺序，两个 thinking 各自留在对应正文/工具前后
    expect(fragments.map((f) => f.type)).toEqual([
      'thinking', 'text', 'tool_call', 'thinking', 'text',
    ])
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

  it('空 thinking part 仍被跳过（不影响其余顺序）', () => {
    const msg = makeMessage([
      { type: 'thinking', content: '', state: 'done' },
      { type: 'text', content: '回复', state: 'done' },
    ])

    const fragments = buildFragmentsFromParts(msg)

    expect(fragments).toHaveLength(1)
    expect(fragments[0].type).toBe('text')
  })
})
