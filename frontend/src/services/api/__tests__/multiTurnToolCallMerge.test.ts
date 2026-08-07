/**
 * 多轮工具调用消息合并回归测试
 *
 * 对应用户反馈 bug：多轮工具调用的对话中，AI 消息只剩一条，tool 消息和中间
 * assistant 消息都不渲染不显示。
 *
 * 正常的多轮工具调用消息流应为：
 *   user → assistant(tool_call 声明) → tool(工具结果) → assistant(基于结果回答)
 *        → tool(第二轮结果) → assistant(最终回答)
 *
 * 回归目标（本次修复的核心行为）：
 *   1. tool 消息必须保留在结果中（不被合并逻辑吸收丢弃）
 *   2. 被 tool 消息分隔的多轮 assistant 不得合并成一条（各自独立显示）
 *   3. 真正连续的 assistant（中间无 tool 分隔）仍可合并（thinking+text 拆分场景）
 */
import { describe, it, expect } from 'vitest'
import { mergeConsecutiveAssistantMessages } from '@/services/api/session'
import type { Message } from '@/types/models'

const SESSION_ID = 'test-session-multi-turn'

function msg(id: string, overrides: Partial<Message> = {}): Message {
  return {
    id,
    sessionId: SESSION_ID,
    sequence: 0,
    role: 'assistant',
    content: '',
    timestamp: '2026-01-01T00:00:00Z',
    parentId: null,
    status: 'completed',
    ...overrides,
  }
}

function toolMsg(
  id: string,
  overrides: Partial<Message> = {},
): Message {
  return msg(id, { role: 'tool', ...overrides })
}

describe('mergeConsecutiveAssistantMessages — 多轮工具调用（回归）', () => {
  it('多轮工具调用消息流完整保留：user + 各轮 assistant + tool + 最终 assistant', () => {
    // 用户反馈的完整场景：2 轮工具调用 + 最终回答
    const messages: Message[] = [
      msg('u1', { role: 'user', content: '帮我查一下天气', sequence: 1 }),
      msg('a1', {
        sequence: 2,
        content: '',
        parts: [
          { type: 'tool_call', callId: 'tc-1', name: 'get_weather', args: { city: '北京' }, state: 'done', sequence: 0 },
        ] as any,
      }),
      toolMsg('t1', {
        sequence: 3,
        content: '北京晴，25度',
        toolCallId: 'tc-1',
        toolName: 'get_weather',
        toolResult: '北京晴，25度',
      }),
      msg('a2', {
        sequence: 4,
        content: '北京今天晴，25度。需要查明天吗？',
        parts: [
          { type: 'text', content: '北京今天晴，25度。需要查明天吗？', state: 'done', sequence: 0 },
          { type: 'tool_call', callId: 'tc-2', name: 'get_weather', args: { city: '北京', date: '明天' }, state: 'done', sequence: 1 },
        ] as any,
      }),
      toolMsg('t2', {
        sequence: 5,
        content: '北京明天多云，22度',
        toolCallId: 'tc-2',
        toolName: 'get_weather',
        toolResult: '北京明天多云，22度',
      }),
      msg('a3', {
        sequence: 6,
        content: '北京明天多云，22度。记得带件外套。',
        parts: [
          { type: 'text', content: '北京明天多云，22度。记得带件外套。', state: 'done', sequence: 0 },
        ] as any,
      }),
    ]

    const merged = mergeConsecutiveAssistantMessages(messages)

    // ★ 核心回归断言 1：全部 6 条消息保留（tool 消息不被吸收丢弃）
    expect(merged).toHaveLength(6)

    // ★ 核心回归断言 2：role 顺序完整 = 用户期望的消息流
    expect(merged.map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
      'tool',
      'assistant',
    ])

    // ★ 核心回归断言 3：各轮 assistant 独立（未被合并成一条）
    const assistants = merged.filter((m) => m.role === 'assistant')
    expect(assistants).toHaveLength(3)
    expect(assistants.map((m) => m.id)).toEqual(['a1', 'a2', 'a3'])
    // 第一轮 assistant 只含 tool_call 声明（content 为空）
    expect(assistants[0].content).toBe('')
    // 第二轮 assistant 含"基于工具结果的回答"
    expect(assistants[1].content).toContain('北京今天晴')
    // 最终 assistant 是完整回答
    expect(assistants[2].content).toContain('北京明天多云')

    // ★ 核心回归断言 4：tool 结果已注入 assistant 的 tool_call part（ActivityCard 数据源）
    const a1ToolCall = assistants[0].parts?.find((p: any) => p.type === 'tool_call')
    expect(a1ToolCall?.result).toBe('北京晴，25度')
    const a2ToolCall = assistants[1].parts?.find((p: any) => p.type === 'tool_call')
    expect(a2ToolCall?.result).toBe('北京明天多云，22度')
  })

  it('单轮工具调用（assistant + tool + assistant）也完整保留，不合并', () => {
    const messages: Message[] = [
      msg('u1', { role: 'user', content: '问题', sequence: 1 }),
      msg('a1', {
        sequence: 2,
        content: '',
        parts: [
          { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 0 },
        ] as any,
      }),
      toolMsg('t1', {
        sequence: 3,
        content: '结果1',
        toolCallId: 'tc-1',
        toolName: 'search',
        toolResult: '结果1',
      }),
      msg('a2', { sequence: 4, content: '基于结果的回答' }),
    ]

    const merged = mergeConsecutiveAssistantMessages(messages)

    expect(merged).toHaveLength(4)
    expect(merged.map((m) => m.role)).toEqual(['user', 'assistant', 'tool', 'assistant'])
    // tool 消息独立保留
    expect(merged[2].role).toBe('tool')
    expect(merged[2].toolResult).toBe('结果1')
    // tool 结果也已注入 a1 的 tool_call part（ActivityCard 数据源）
    const a1ToolCall = (merged[1] as Message).parts?.find((p: any) => p.type === 'tool_call')
    expect(a1ToolCall?.result).toBe('结果1')
    // assistant 不合并：a1（声明）和 a2（回答）各自独立
    expect(merged[1].id).toBe('a1')
    expect(merged[3].id).toBe('a2')
  })

  it('真正连续的 assistant（无 tool 分隔）仍合并（thinking+text 拆分场景不受影响）', () => {
    const messages: Message[] = [
      msg('u1', { role: 'user', content: '问题', sequence: 1 }),
      msg('a1', {
        sequence: 2,
        content: '第一部分',
        parts: [
          { type: 'text', content: '第一部分', state: 'done', sequence: 0 },
        ] as any,
      }),
      msg('a2', {
        sequence: 3,
        content: '第二部分',
        parts: [
          { type: 'text', content: '第二部分', state: 'done', sequence: 0 },
        ] as any,
      }),
    ]

    const merged = mergeConsecutiveAssistantMessages(messages)

    // 无 tool 分隔的连续 assistant 仍合并（保持原有合并能力）
    expect(merged).toHaveLength(2)
    expect(merged[0].role).toBe('user')
    expect(merged[1].role).toBe('assistant')
    expect(merged[1].content).toContain('第一部分')
    expect(merged[1].content).toContain('第二部分')
  })
})
