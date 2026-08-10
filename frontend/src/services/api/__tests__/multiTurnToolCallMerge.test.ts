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

    // ★ 核心回归断言 1：一个对话轮次合并为一个气泡 = user + 1 条合并 assistant
    expect(merged).toHaveLength(2)
    expect(merged.map((m) => m.role)).toEqual(['user', 'assistant'])

    // ★ 核心回归断言 2：合并的 assistant 保留全部多轮内容（与流式一致：多轮工具调用
    //   在流式时天然是一个气泡，tool_call part 追加在同一个 assistant 的 parts 里）
    const bubble = merged[1]
    // 文本拼接：三轮 assistant 的 content 都在
    expect(bubble.content).toContain('北京今天晴')
    expect(bubble.content).toContain('北京明天多云')
    expect(bubble.content).toContain('记得带件外套')
    // parts 保留所有 tool_call（两个工具调用 + 文本片段）
    const toolCalls = (bubble.parts || []).filter((p: any) => p.type === 'tool_call')
    expect(toolCalls).toHaveLength(2)
    // tool 结果已注入各自的 tool_call part（ActivityCard 数据源）
    expect(toolCalls[0].callId).toBe('tc-1')
    expect(toolCalls[0].result).toBe('北京晴，25度')
    expect(toolCalls[1].callId).toBe('tc-2')
    expect(toolCalls[1].result).toBe('北京明天多云，22度')
    // parts 顺序：保留各消息的原始顺序（a1 声明 tool_call → a2 文本+下一轮 tool_call → a3 文本）
    const partTypes = (bubble.parts || []).map((p: any) => p.type)
    expect(partTypes.filter((t) => t === 'text' || t === 'tool_call')).toEqual([
      'tool_call', 'text', 'tool_call', 'text',
    ])
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

    // 单轮工具调用也合并为一个气泡：user + 1 条 assistant（含 tool_call part + 文本）
    expect(merged).toHaveLength(2)
    expect(merged.map((m) => m.role)).toEqual(['user', 'assistant'])
    // tool 结果已注入 a1 的 tool_call part（ActivityCard 数据源）
    const a1ToolCall = (merged[1] as Message).parts?.find((p: any) => p.type === 'tool_call')
    expect(a1ToolCall?.result).toBe('结果1')
    // 合并后的气泡保留两轮文本
    expect((merged[1] as Message).content).toContain('基于结果的回答')
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
