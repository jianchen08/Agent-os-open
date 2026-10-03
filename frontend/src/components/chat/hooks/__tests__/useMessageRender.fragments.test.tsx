// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * useMessageRender 分片基础映射特征化测试（消息流渲染器化改造护栏，2026-10-03）。
 *
 * 锁 parts[] → fragments 的基础类型映射（正门；顺序与边缘已有
 * MessageOrderVerification / thinkingOrderFix / residual 专项）：
 * - text → text fragment（content 透传、sourceId=message.id）
 * - 最后一个 text fragment isLast=true，其余（含被 tool/thinking 隔断的）false
 * - thinking → thinking fragment（isThinking=state==='streaming'，steps/durationMs 透传）
 * - tool_call → tool_call fragment（state 四态映射 + index/total 递增）
 * - displayContent：有 parts 时 = 全部 text fragment 拼接（编辑/复制的内容源）；
 *   无 parts 时 = versionContent ?? message.content
 */
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import useMessageRender from '../useMessageRender'
import type { Message } from '@/types/models'
import type { MessagePart } from '@/types/messageParts'

function makeMessage(parts: MessagePart[], content = ''): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'assistant',
    content,
    timestamp: new Date().toISOString(),
    status: 'completed',
    parts,
  }
}

function renderFragments(message: Message, options?: { versionContent?: string | null }) {
  return renderHook(() =>
    useMessageRender({ message, isLast: false, isGenerating: false, ...options }),
  )
}

describe('useMessageRender 分片基础映射', () => {
  it('text part → text fragment：content 透传、sourceId=message.id', () => {
    const { result } = renderFragments(
      makeMessage([{ type: 'text', content: '你好', sequence: 1 } as MessagePart]),
    )
    expect(result.current.fragments).toHaveLength(1)
    const f = result.current.fragments[0]
    expect(f.type).toBe('text')
    if (f.type === 'text') {
      expect(f.content).toBe('你好')
      expect(f.sourceId).toBe('msg-1')
    }
  })

  it('唯一 text fragment → isLast=true；多段 text 时仅最后一段 isLast', () => {
    const { result } = renderFragments(
      makeMessage([
        { type: 'text', content: 'first', sequence: 1 },
        { type: 'text', content: 'second', sequence: 2 },
      ] as MessagePart[]),
    )
    const texts = result.current.fragments.filter((f) => f.type === 'text')
    expect(texts.map((f) => (f.type === 'text' ? f.isLast : null))).toEqual([false, true])
  })

  it('text 被 tool_call 隔断 → isLast 落在最后一个 text（非最后一个 fragment）', () => {
    const { result } = renderFragments(
      makeMessage([
        { type: 'text', content: 'before', sequence: 1 },
        {
          type: 'tool_call',
          callId: 'c1',
          name: 'bash',
          args: '{}',
          state: 'done',
          sequence: 2,
        } as MessagePart,
        { type: 'text', content: 'after', sequence: 3 },
      ] as MessagePart[]),
    )
    const byType = result.current.fragments.map((f) => f.type)
    expect(byType).toEqual(['text', 'tool_call', 'text'])
    const first = result.current.fragments[0]
    const last = result.current.fragments[2]
    expect(first.type === 'text' && first.isLast).toBe(false)
    expect(last.type === 'text' && last.isLast).toBe(true)
  })

  it('thinking part → thinking fragment：isThinking=state streaming，steps/durationMs 透传', () => {
    const { result } = renderFragments(
      makeMessage([
        {
          type: 'thinking',
          content: '让我想想',
          state: 'streaming',
          durationMs: 1200,
          steps: ['a', 'b'],
          sequence: 1,
        } as MessagePart,
      ]),
    )
    const f = result.current.fragments[0]
    expect(f.type).toBe('thinking')
    if (f.type === 'thinking') {
      expect(f.thinking.content).toBe('让我想想')
      expect(f.thinking.isThinking).toBe(true)
      expect(f.thinking.durationMs).toBe(1200)
      expect(f.thinking.steps).toEqual(['a', 'b'])
    }
  })

  it('tool_call state 四态映射：calling→running / done→completed / error→failed / cancelled→cancelled', () => {
    const states = ['calling', 'done', 'error', 'cancelled'] as const
    const expected = ['running', 'completed', 'failed', 'cancelled']
    states.forEach((state, i) => {
      const { result } = renderFragments(
        makeMessage([
          { type: 'tool_call', callId: `c-${i}`, name: 'bash', args: '{}', state, sequence: 1 } as MessagePart,
        ]),
      )
      const f = result.current.fragments[0]
      expect(f.type).toBe('tool_call')
      if (f.type === 'tool_call') {
        expect(f.toolCall.status).toBe(expected[i])
        expect(f.activity.status).toBe(expected[i])
      }
    })
  })

  it('多个 tool_call：index 依次递增、total=总数', () => {
    const { result } = renderFragments(
      makeMessage([
        { type: 'tool_call', callId: 'c1', name: 'bash', args: '{}', state: 'done', sequence: 1 },
        { type: 'tool_call', callId: 'c2', name: 'bash', args: '{}', state: 'done', sequence: 2 },
        { type: 'tool_call', callId: 'c3', name: 'bash', args: '{}', state: 'done', sequence: 3 },
      ] as MessagePart[]),
    )
    const tools = result.current.fragments.filter((f) => f.type === 'tool_call')
    expect(tools.map((f) => (f.type === 'tool_call' ? f.index : -1))).toEqual([0, 1, 2])
    expect(tools.every((f) => f.type === 'tool_call' && f.total === 3)).toBe(true)
  })

  it('displayContent：有 parts 时 = 全部 text fragment 拼接（编辑/复制的内容源）', () => {
    const { result } = renderFragments(
      makeMessage(
        [
          { type: 'text', content: '第一段', sequence: 1 },
          { type: 'text', content: '第二段', sequence: 2 },
        ] as MessagePart[],
        '整体 content',
      ),
    )
    expect(result.current.displayContent).toBe('第一段第二段')
  })

  it('displayContent：无 parts 时 = versionContent ?? message.content', () => {
    const bare = makeMessage([], '裸 content')
    bare.parts = undefined
    const plain = renderFragments(bare)
    expect(plain.result.current.displayContent).toBe('裸 content')

    const edited = renderFragments(bare, { versionContent: '编辑后的版本' })
    expect(edited.result.current.displayContent).toBe('编辑后的版本')
  })
})
