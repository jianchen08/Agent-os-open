/** mergeConsecutiveAssistantMessages 纯函数测试 所有现有 store 测试将此函数 mock 为恒等， */

import { describe, it, expect } from 'vitest'
import { mergeConsecutiveAssistantMessages } from '@/services/api/session'
import type { Message } from '@/types/models'

const SESSION_ID = 'test-session-1'

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

describe('mergeConsecutiveAssistantMessages', () => {
  describe('sequence 保持（修复核心）', () => {
    it('保留流式大数 sequence（Date.now() 风格）', () => {
      const flowSeq = Date.now() // 大毫秒数
      const messages: Message[] = [
        msg('ai-1', {
          content: '部分1',
          sequence: 1,
          parts: [
            { type: 'thinking', content: '思考中...', state: 'done', sequence: flowSeq },
            { type: 'text', content: '部分1', state: 'done', sequence: flowSeq + 1 },
          ] as any,
        }),
        msg('ai-2', {
          content: '部分2',
          sequence: 2,
          parts: [
            { type: 'text', content: '部分2', state: 'done', sequence: flowSeq + 2 },
          ] as any,
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)

      const parts = merged[0].parts as any[]
      expect(parts).toHaveLength(3)

      // 原始大数 sequence 应当被保留（无冲突）
      const seqs = parts.map((p) => p.sequence as number)
      expect(seqs).toContain(flowSeq)
      expect(seqs).toContain(flowSeq + 1)
      expect(seqs).toContain(flowSeq + 2)
    })

    it('两条 API 消息各自 parts 从 0 起算时做局部冲突消除', () => {
      const messages: Message[] = [
        msg('ai-1', {
          content: '思考+文本',
          sequence: 1,
          parts: [
            { type: 'thinking', content: '思考', state: 'done', sequence: 0 },
            { type: 'text', content: '文本', state: 'done', sequence: 1 },
          ] as any,
        }),
        msg('ai-2', {
          content: '工具结果',
          sequence: 2,
          parts: [
            { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 0 },
            { type: 'text', content: '总结', state: 'done', sequence: 1 },
          ] as any,
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)

      const parts = merged[0].parts as any[]
      expect(parts).toHaveLength(4)

      // 所有 part sequence 应唯一（冲突的被续接）
      const uniqueSeqs = new Set(parts.map((p) => p.sequence as number))
      expect(uniqueSeqs.size).toBe(parts.length)

      // thinking 和 text 的原始 sequence 0/1 被保留
      const think = parts.find((p: any) => p.type === 'thinking')
      const firstText = parts.find((p: any) => p.type === 'text' && p.content === '文本')
      expect(think).toBeDefined()
      expect(firstText).toBeDefined()
      expect(think.sequence).toBe(0)
      expect(firstText.sequence).toBe(1)

      // tool_call 和第二个 text 因冲突被续接（sequence > 1）
      const toolCall = parts.find((p: any) => p.type === 'tool_call')
      const secondText = parts.find((p: any) => p.type === 'text' && p.content === '总结')
      expect(toolCall).toBeDefined()
      expect(secondText).toBeDefined()
      expect(toolCall.sequence).toBeGreaterThan(1)
      expect(secondText.sequence).toBeGreaterThan(1)
      expect(toolCall.sequence).not.toBe(secondText.sequence)
    })

    it('单条 assistant 消息保持不变', () => {
      const messages: Message[] = [
        msg('ai-1', {
          content: '单条回复',
          sequence: 1,
          parts: [
            { type: 'thinking', content: '思考', state: 'done', sequence: 0 },
            { type: 'text', content: '回复', state: 'done', sequence: 1 },
          ] as any,
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)
      const parts = merged[0].parts as any[]
      expect(parts).toHaveLength(2)
      expect(parts[0].sequence).toBe(0)
      expect(parts[1].sequence).toBe(1)
    })

    it('思考+文本顺序不因合并而错乱', () => {
      const messages: Message[] = [
        msg('ai-1', {
          content: '思考过程',
          sequence: 1,
          parts: [
            { type: 'thinking', content: '第一步思考', state: 'done', sequence: 0 },
            { type: 'text', content: '最终回复', state: 'done', sequence: 1 },
          ] as any,
        }),
        msg('ai-2', {
          content: '补充',
          sequence: 2,
          parts: [
            { type: 'text', content: '补充内容', state: 'done', sequence: 0 },
          ] as any,
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)

      const parts = merged[0].parts as any[]
      expect(parts).toHaveLength(3)

      // ai-1 的 thinking(seq=0) 和 text(seq=1) 无冲突，原始 sequence 保留
      // ai-2 的 text(seq=0) 冲突，续接为 >1
      const think = parts.find((p: any) => p.type === 'thinking')
      const firstText = parts.find((p: any) => p.type === 'text' && p.content === '最终回复')
      const secondText = parts.find((p: any) => p.type === 'text' && p.content === '补充内容')
      expect(think).toBeDefined()
      expect(firstText).toBeDefined()
      expect(secondText).toBeDefined()
      expect(think.sequence).toBe(0)
      expect(firstText.sequence).toBe(1)
      expect(secondText.sequence).toBeGreaterThan(1)
    })
  })

  describe('跨消息 part 逻辑顺序', () => {
    it('两条 assistant 各有 thinking+text 时，思考紧跟其回复（不被全局排序打散）', () => {
      // 多条 API 消息各自 parts 从 0 起算，旧版全局排序导致思考与回复「分家」
      const messages: Message[] = [
        msg('ai-1', {
          content: '回复A',
          sequence: 1,
          parts: [
            { type: 'thinking', content: 'A的思考', state: 'done', sequence: 0 } as any,
            { type: 'text', content: '回复A', state: 'done', sequence: 1 } as any,
          ],
        }),
        msg('ai-2', {
          content: '回复B',
          sequence: 2,
          parts: [
            { type: 'thinking', content: 'B的思考', state: 'done', sequence: 0 } as any,
            { type: 'text', content: '回复B', state: 'done', sequence: 1 } as any,
          ],
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)
      const parts = merged[0].parts as any[]

      // 逻辑顺序：A的思考 → 回复A → B的思考 → 回复B（思考紧跟其回复）
      expect(parts.map((p) => `${p.type}:${p.content}`)).toEqual([
        'thinking:A的思考',
        'text:回复A',
        'thinking:B的思考',
        'text:回复B',
      ])
      // sequence 单调递增，渲染层按数值排序后与逻辑顺序一致
      const seqs = parts.map((p) => p.sequence as number)
      for (let i = 1; i < seqs.length; i++) {
        expect(seqs[i]).toBeGreaterThan(seqs[i - 1])
      }
    })

    it('含 tool_call 的跨消息顺序：思考→工具→文本 各自归位', () => {
      const messages: Message[] = [
        msg('ai-1', {
          content: '回复A',
          sequence: 1,
          parts: [
            { type: 'thinking', content: 'A思考', state: 'done', sequence: 0 } as any,
            { type: 'text', content: '回复A', state: 'done', sequence: 1 } as any,
          ],
        }),
        msg('ai-2', {
          content: '回复B',
          sequence: 2,
          parts: [
            { type: 'thinking', content: 'B思考', state: 'done', sequence: 0 } as any,
            { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 1 } as any,
            { type: 'text', content: '回复B', state: 'done', sequence: 2 } as any,
          ],
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)
      const parts = merged[0].parts as any[]

      // 期望顺序：A思考 → 回复A → B思考 → 工具 → 回复B
      expect(parts.map((p) => `${p.type}:${p.content || p.name || ''}`)).toEqual([
        'thinking:A思考',
        'text:回复A',
        'thinking:B思考',
        'tool_call:search',
        'text:回复B',
      ])
    })

    it('流式大数 sequence（无冲突）原样保留，顺序不变', () => {
      // 流式消息 parts 用 Date.now() 大数，不冲突时不应被改动
      const flowSeq = Date.now()
      const messages: Message[] = [
        msg('ai-1', {
          content: '回复',
          sequence: 1,
          parts: [
            { type: 'thinking', content: '思考', state: 'done', sequence: flowSeq } as any,
            { type: 'text', content: '回复', state: 'done', sequence: flowSeq + 1 } as any,
          ],
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      const parts = merged[0].parts as any[]
      expect(parts.map((p) => p.sequence)).toEqual([flowSeq, flowSeq + 1])
    })
  })

  describe('tool_call 吸收', () => {
    it('将 tool 消息的结果注入前一个 assistant 的 tool_call part', () => {
      const messages: Message[] = [
        msg('ai-1', {
          content: '',
          sequence: 1,
          parts: [
            { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'streaming' } as any,
            { type: 'text', content: '查询中...', state: 'streaming', sequence: 0 } as any,
          ] as any,
        }),
        {
          id: 'tool-1',
          sessionId: SESSION_ID,
          sequence: 2,
          role: 'tool' as Message['role'],
          content: '',
          timestamp: '2026-01-01T00:00:00Z',
          parentId: null,
          toolCallId: 'tc-1',
          toolName: 'search',
          toolResult: '搜索结果：...',
          durationMs: 500,
        } as Message,
        msg('ai-2', {
          content: '最终回复',
          sequence: 3,
          parts: [
            { type: 'text', content: '最终回复', state: 'done', sequence: 0 } as any,
          ] as any,
        }),
      ]

      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)

      const parts = merged[0].parts as any[]
      const toolCallPart = parts.find((p: any) => p.type === 'tool_call')
      expect(toolCallPart).toBeDefined()
      expect(toolCallPart.result).toBe('搜索结果：...')
      expect(toolCallPart.durationMs).toBe(500)
      expect(toolCallPart.state).toBe('done')
    })
  })

  describe('边界情况', () => {
    it('空数组返回空数组', () => {
      expect(mergeConsecutiveAssistantMessages([])).toEqual([])
    })

    it('单条消息直接返回', () => {
      const m = [msg('ai-1', { content: 'hi', sequence: 1 })]
      expect(mergeConsecutiveAssistantMessages(m)).toHaveLength(1)
    })

    it('非 assistant 消息原样保留', () => {
      const messages: Message[] = [
        msg('u-1', { role: 'user', content: '你好', sequence: 1 }),
        msg('ai-1', { content: '你好！', sequence: 2 }),
      ]
      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(2)
      expect(merged[0].role).toBe('user')
      expect(merged[1].role).toBe('assistant')
    })

    it('无 parts 的消息合并时保留 content', () => {
      const messages: Message[] = [
        msg('ai-1', { content: '第一部分', sequence: 1 }),
        msg('ai-2', { content: '第二部分', sequence: 2 }),
      ]
      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)
      expect(merged[0].content).toBe('第一部分\n\n第二部分')
    })

    it('一条有 parts 一条无 parts 不冲突', () => {
      const messages: Message[] = [
        msg('ai-1', {
          content: '',
          sequence: 1,
          parts: [
            { type: 'text', content: '有 parts 的回复', state: 'done', sequence: 0 },
          ] as any,
        }),
        msg('ai-2', { content: '纯文本回复', sequence: 2 }),
      ]
      const merged = mergeConsecutiveAssistantMessages(messages)
      expect(merged).toHaveLength(1)
      expect(merged[0].parts).toBeDefined()
      expect((merged[0].parts as any[]).length).toBeGreaterThanOrEqual(1)
      expect(merged[0].content).toContain('纯文本回复')
    })
  })
})
