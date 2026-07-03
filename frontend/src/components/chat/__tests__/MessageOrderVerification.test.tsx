/**
 * MessageOrderVerification.test.tsx
 *
 * 验证 AC-1b: 所有流程中消息渲染顺序正确、无错乱
 *
 * 测试覆盖：
 * 1. 文本+工具+文本 顺序
 * 2. 思考+工具+文本 混合顺序
 * 3. 多工具调用顺序
 * 4. 动态 parts 更新
 * 5. isLast 标记正确性
 * 6. 流式输出时顺序不变
 * 7. 空 parts 处理
 */

import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMessageRender } from '@/components/chat/hooks/useMessageRender'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import type { Message, MessageToolCall } from '@/types/models'
import type { MessagePart } from '@/types/messageParts'

// ---------------------------------------------------------------------------
//  Mock: activityConverter（useMessageRender 内部依赖）
// ---------------------------------------------------------------------------
vi.mock('@/utils/activityConverter', () => ({
  toolCallToActivity: (toolCall: MessageToolCall) => ({
    type: 'tool_call',
    id: toolCall.call_id,
    title: toolCall.tool_name,
    toolName: toolCall.tool_name,
    status: toolCall.status,
    details: [],
    actions: [],
  }),
}))

// ---------------------------------------------------------------------------
//  Mock: toolCardRegistry（activityConverter 内部依赖）
// ---------------------------------------------------------------------------
vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (base: any) => base,
}))

// ---------------------------------------------------------------------------
//  工厂函数
// ---------------------------------------------------------------------------

/** 创建基础 Message */
function createMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    ...overrides,
  }
}

/** 创建文本 Part */
function textPart(content: string, sequence: number = 1): MessagePart {
  return { type: 'text', content, state: 'done', sequence }
}

/** 创建思考 Part */
function thinkingPart(content: string, sequence: number = 1): MessagePart {
  return { type: 'thinking', content, state: 'done', sequence }
}

/** 创建工具调用 Part */
function toolCallPart(
  callId: string,
  name: string,
  state: 'streaming' | 'calling' | 'done' | 'error' | 'cancelled' = 'done',
  sequence: number = 1,
): MessagePart {
  return {
    type: 'tool_call',
    callId,
    name,
    args: {},
    state,
    sequence,
  }
}

/** 提取 fragments 类型和顺序 */
function extractFragmentTypes(fragments: RenderFragment[]): string[] {
  return fragments.map((f) => f.type)
}

// ---------------------------------------------------------------------------
//  测试
// ---------------------------------------------------------------------------

describe('MessageOrderVerification — AC-1b: 消息渲染顺序正确', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // -----------------------------------------------------------------------
  // 1. 文本+工具+文本 顺序
  // -----------------------------------------------------------------------
  describe('文本+工具+文本 顺序', () => {
    it('parts: text → tool_call → text 时 fragments 顺序一致', () => {
      const message = createMessage({
        parts: [
          textPart('分析中', 1),
          toolCallPart('tc-1', 'read_file', 'done', 2),
          textPart('完成', 3),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const types = extractFragmentTypes(result.current.fragments)
      expect(types).toEqual(['text', 'tool_call', 'text'])
    })

    it('fragments 内容与 parts 对应', () => {
      const message = createMessage({
        parts: [
          textPart('分析中', 1),
          toolCallPart('tc-1', 'read_file', 'done', 2),
          textPart('完成', 3),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const fragments = result.current.fragments
      expect(fragments[0]).toMatchObject({ type: 'text', content: '分析中' })
      expect(fragments[1]).toMatchObject({
        type: 'tool_call',
        toolCall: { call_id: 'tc-1', tool_name: 'read_file' },
      })
      expect(fragments[2]).toMatchObject({ type: 'text', content: '完成' })
    })
  })

  // -----------------------------------------------------------------------
  // 2. 思考+工具+文本 混合顺序
  // -----------------------------------------------------------------------
  describe('思考+工具+文本 混合顺序', () => {
    it('parts: thinking → text → tool_call → text 时顺序一致', () => {
      const message = createMessage({
        parts: [
          thinkingPart('让我想想...', 1),
          textPart('使用工具分析', 2),
          toolCallPart('tc-1', 'search', 'done', 3),
          textPart('结果如下', 4),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const types = extractFragmentTypes(result.current.fragments)
      expect(types).toEqual(['thinking', 'text', 'tool_call', 'text'])
    })

    it('thinking fragment 包含思考内容', () => {
      const message = createMessage({
        parts: [
          thinkingPart('分析问题中...', 1),
          textPart('结论', 2),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const thinking = result.current.fragments[0]
      expect(thinking.type).toBe('thinking')
      if (thinking.type === 'thinking') {
        expect(thinking.thinking.content).toBe('分析问题中...')
      }
    })
  })

  // -----------------------------------------------------------------------
  // 3. 多工具调用顺序
  // -----------------------------------------------------------------------
  describe('多工具调用顺序', () => {
    it('三个 tool_call 的 index 和 total 正确', () => {
      const message = createMessage({
        parts: [
          toolCallPart('tc-1', 'read_file', 'done', 1),
          toolCallPart('tc-2', 'write_file', 'done', 2),
          toolCallPart('tc-3', 'execute', 'done', 3),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const fragments = result.current.fragments
      expect(fragments).toHaveLength(3)

      // 验证 index 和 total
      for (let i = 0; i < fragments.length; i++) {
        const f = fragments[i]
        expect(f.type).toBe('tool_call')
        if (f.type === 'tool_call') {
          expect(f.index).toBe(i)
          expect(f.total).toBe(3)
        }
      }
    })

    it('tool_call 顺序与 parts 中出现顺序一致', () => {
      const message = createMessage({
        parts: [
          textPart('准备中', 1),
          toolCallPart('tc-a', 'tool_a', 'done', 2),
          textPart('中间', 3),
          toolCallPart('tc-b', 'tool_b', 'done', 4),
          textPart('结束', 5),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const toolFragments = result.current.fragments.filter(
        (f) => f.type === 'tool_call',
      )
      expect(toolFragments).toHaveLength(2)

      if (toolFragments[0].type === 'tool_call') {
        expect(toolFragments[0].toolCall.call_id).toBe('tc-a')
        expect(toolFragments[0].index).toBe(0)
      }
      if (toolFragments[1].type === 'tool_call') {
        expect(toolFragments[1].toolCall.call_id).toBe('tc-b')
        expect(toolFragments[1].index).toBe(1)
      }
      // total = 2
      if (toolFragments[0].type === 'tool_call') {
        expect(toolFragments[0].total).toBe(2)
      }
    })
  })

  // -----------------------------------------------------------------------
  // 3.5 part 渲染顺序 = 数组顺序（与 sequence 数值无关）
  //    简化重构：渲染层不再按 sequence 排序，直接按 parts 数组顺序遍历。
  //    流式新建的 part 不赋 sequence；历史消息 part 在 API 映射时按顺序编号。
  //    这样彻底消除「fallback 大数把 part 永久推到末尾」的工具卡片常驻底部 bug。
  // -----------------------------------------------------------------------
  describe('part 渲染顺序 = 数组顺序', () => {
    it('parts 按 sequence 升序排列时，渲染顺序与数组顺序一致', () => {
      const message = createMessage({
        parts: [
          textPart('调用前', 1),
          toolCallPart('tc-1', 'read_file', 'done', 2),
          textPart('调用后', 3),
        ],
      })

      const { result } = renderHook(() => useMessageRender({ message }))

      expect(extractFragmentTypes(result.current.fragments)).toEqual(['text', 'tool_call', 'text'])
    })

    it('sequence 数值乱序时，渲染仍按数组顺序（sequence 不影响渲染）', () => {
      // 工具卡片的 sequence 故意是天文数字（模拟旧的 Date.now() fallback 残留），
      // 后续文本 sequence 是小数。重构后渲染按数组顺序，工具卡片不再被推到末尾。
      const message = createMessage({
        parts: [
          textPart('调用前', 1),
          toolCallPart('tc-1', 'read_file', 'done', 9999999999999),
          textPart('调用后', 2),
        ],
      })

      const { result } = renderHook(() => useMessageRender({ message }))

      expect(extractFragmentTypes(result.current.fragments)).toEqual(['text', 'tool_call', 'text'])
    })

    it('流式新建的 part 无 sequence 字段时，渲染按数组顺序（即接收顺序）', () => {
      // 模拟流式真实数据：part 上根本没有 sequence 字段
      const message = createMessage({
        parts: [
          { type: 'thinking', content: '思考中', state: 'done' } as MessagePart,
          { type: 'text', content: '调用前', state: 'done' } as MessagePart,
          { type: 'tool_call', callId: 'tc-1', name: 'read_file', args: {}, state: 'done' } as MessagePart,
          { type: 'text', content: '调用后', state: 'streaming' } as MessagePart,
        ],
      })

      const { result } = renderHook(() => useMessageRender({ message }))

      expect(extractFragmentTypes(result.current.fragments)).toEqual(['thinking', 'text', 'tool_call', 'text'])
    })
  })

  // -----------------------------------------------------------------------
  // 4. 动态 parts 更新
  // -----------------------------------------------------------------------
  describe('动态 parts 更新', () => {
    it('parts 增长时 fragments 数量同步增长', () => {
      const initialParts: MessagePart[] = [textPart('分析中', 1)]

      const { result, rerender } = renderHook(
        ({ parts }: { parts: MessagePart[] }) =>
          useMessageRender({
            message: createMessage({ parts }),
          }),
        { initialProps: { parts: initialParts } },
      )

      // 初始 1 个 fragment
      expect(result.current.fragments).toHaveLength(1)
      expect(result.current.fragments[0]).toMatchObject({
        type: 'text',
        content: '分析中',
      })

      // 追加一个 tool_call
      const updatedParts: MessagePart[] = [
        textPart('分析中', 1),
        toolCallPart('tc-1', 'search', 'done', 2),
      ]
      rerender({ parts: updatedParts })

      expect(result.current.fragments).toHaveLength(2)
      expect(result.current.fragments[0]).toMatchObject({ type: 'text' })
      expect(result.current.fragments[1]).toMatchObject({ type: 'tool_call' })
    })

    it('parts 从 2 个增长到 4 个时顺序不变', () => {
      const { result, rerender } = renderHook(
        ({ parts }: { parts: MessagePart[] }) =>
          useMessageRender({
            message: createMessage({ parts }),
          }),
        {
          initialProps: {
            parts: [
              textPart('第一步', 1),
              toolCallPart('tc-1', 'tool_a', 'done', 2),
            ],
          },
        },
      )

      expect(extractFragmentTypes(result.current.fragments)).toEqual([
        'text',
        'tool_call',
      ])

      rerender({
        parts: [
          textPart('第一步', 1),
          toolCallPart('tc-1', 'tool_a', 'done', 2),
          textPart('中间步骤', 3),
          toolCallPart('tc-2', 'tool_b', 'done', 4),
        ],
      })

      expect(extractFragmentTypes(result.current.fragments)).toEqual([
        'text',
        'tool_call',
        'text',
        'tool_call',
      ])
    })
  })

  // -----------------------------------------------------------------------
  // 5. isLast 标记正确性
  // -----------------------------------------------------------------------
  describe('isLast 标记正确性', () => {
    it('最后一个 text fragment 的 isLast = true', () => {
      const message = createMessage({
        parts: [
          textPart('第一段', 1),
          toolCallPart('tc-1', 'search', 'done', 2),
          textPart('第二段', 3),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const textFragments = result.current.fragments.filter(
        (f) => f.type === 'text',
      )
      expect(textFragments).toHaveLength(2)

      // 第一个 text isLast = false
      expect(textFragments[0]).toMatchObject({
        type: 'text',
        isLast: false,
      })
      // 最后一个 text isLast = true
      expect(textFragments[1]).toMatchObject({
        type: 'text',
        isLast: true,
      })
    })

    it('仅有 text 时最后一个 isLast = true', () => {
      const message = createMessage({
        parts: [textPart('内容', 1)],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      expect(result.current.fragments[0]).toMatchObject({
        type: 'text',
        isLast: true,
      })
    })

    it('无 text 时无 isLast=true 的 fragment', () => {
      const message = createMessage({
        parts: [toolCallPart('tc-1', 'tool', 'done', 1)],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const textFragments = result.current.fragments.filter(
        (f) => f.type === 'text',
      )
      expect(textFragments).toHaveLength(0)
    })
  })

  // -----------------------------------------------------------------------
  // 6. 流式输出时顺序不变
  // -----------------------------------------------------------------------
  describe('流式输出时顺序不变', () => {
    it('isStreaming=true 时 parts 持续追加顺序正确', () => {
      const { result, rerender } = renderHook(
        ({ parts, isGenerating }: { parts: MessagePart[]; isGenerating: boolean }) =>
          useMessageRender({
            message: createMessage({
              parts,
              role: 'assistant',
            }),
            isLast: true,
            isGenerating,
          }),
        {
          initialProps: {
            parts: [textPart('开始', 1)],
            isGenerating: true,
          },
        },
      )

      // 第一轮
      expect(extractFragmentTypes(result.current.fragments)).toEqual(['text'])
      expect(result.current.isStreaming).toBe(true)

      // 第二轮：追加 tool_call
      rerender({
        parts: [textPart('开始', 1), toolCallPart('tc-1', 'search', 'done', 2)],
        isGenerating: true,
      })
      expect(extractFragmentTypes(result.current.fragments)).toEqual([
        'text',
        'tool_call',
      ])

      // 第三轮：追加 text
      rerender({
        parts: [
          textPart('开始', 1),
          toolCallPart('tc-1', 'search', 'done', 2),
          textPart('结果', 3),
        ],
        isGenerating: true,
      })
      expect(extractFragmentTypes(result.current.fragments)).toEqual([
        'text',
        'tool_call',
        'text',
      ])

      // 结束流
      rerender({
        parts: [
          textPart('开始', 1),
          toolCallPart('tc-1', 'search', 'done', 2),
          textPart('结果', 3),
        ],
        isGenerating: false,
      })
      expect(result.current.isStreaming).toBe(false)
    })
  })

  // -----------------------------------------------------------------------
  // 7. 空 parts 处理
  // -----------------------------------------------------------------------
  describe('空 parts 处理', () => {
    it('parts 包含 thinking + tool_call + text 时 fragments 正确构建', () => {
      const message = createMessage({
        content: '这是纯文本内容',
        parts: [
          thinkingPart('思考中', 1),
          toolCallPart('tc-1', 'search', 'done', 2),
          textPart('这是纯文本内容', 3),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      // parts 直接构建 fragments: thinking → tool_call → text
      const types = extractFragmentTypes(result.current.fragments)
      expect(types).toEqual(['thinking', 'tool_call', 'text'])

      // 验证内容
      const fragments = result.current.fragments
      if (fragments[0].type === 'thinking') {
        expect(fragments[0].thinking.content).toBe('思考中')
      }
      if (fragments[1].type === 'tool_call') {
        expect(fragments[1].toolCall.tool_name).toBe('search')
      }
      if (fragments[2].type === 'text') {
        expect(fragments[2].content).toBe('这是纯文本内容')
      }
    })

    it('空 parts 数组返回空 fragments', () => {
      const message = createMessage({
        content: '文本',
        parts: [],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      expect(result.current.fragments).toHaveLength(0)
    })

    it('仅有 text part 时返回单个 text fragment', () => {
      const message = createMessage({
        content: '纯文本消息',
        parts: [textPart('纯文本消息', 1)],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      expect(result.current.fragments).toHaveLength(1)
      expect(result.current.fragments[0]).toMatchObject({
        type: 'text',
        content: '纯文本消息',
        isLast: true,
      })
    })

    it('仅有 tool_call parts 时返回 tool_call fragments', () => {
      const message = createMessage({
        content: '',
        parts: [
          toolCallPart('tc-1', 'tool_a', 'done', 1),
          toolCallPart('tc-2', 'tool_b', 'done', 2),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const types = extractFragmentTypes(result.current.fragments)
      expect(types).toEqual(['tool_call', 'tool_call'])
    })
  })
})
