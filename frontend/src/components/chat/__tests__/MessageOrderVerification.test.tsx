/**
 * MessageOrderVerification.test.tsx
 *
 * 验证 AC-1b: 所有流程中消息渲染顺序正确、无错乱
 *
 * 测试覆盖：
 * 1. 文本+工具+文本 顺序
 * 2. 思考+工具+文本 混合顺序
 * 3. 多工具调用顺序
 * 4. 动态 contentBlocks 更新
 * 5. isLast 标记正确性
 * 6. 流式输出时顺序不变
 * 7. 空 contentBlocks 降级
 */

import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useMessageRender } from '@/components/chat/hooks/useMessageRender'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import type {
  ContentBlock,
  Message,
  MessageToolCall,
  ThinkingContent,
} from '@/types/models'

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

/** 创建 text ContentBlock */
function textBlock(text: string, sourceId = 'msg-1'): ContentBlock {
  return { type: 'text', text, sourceId }
}

/** 创建 thinking ContentBlock */
function thinkingBlock(content: string, sourceId = 'msg-1'): ContentBlock {
  return {
    type: 'thinking',
    thinking: { content, isThinking: false },
    sourceId,
  }
}

/** 创建 tool_call ContentBlock */
function toolCallBlock(
  callId: string,
  toolName: string,
  status: MessageToolCall['status'] = 'completed',
): ContentBlock {
  return {
    type: 'tool_call',
    toolCall: {
      call_id: callId,
      tool_name: toolName,
      tool_args: {},
      status,
    },
    sourceId: 'msg-1',
  }
}

/** 创建 MessageToolCall（用于降级测试） */
function createToolCall(
  callId: string,
  toolName: string,
  status: MessageToolCall['status'] = 'completed',
): MessageToolCall {
  return {
    call_id: callId,
    tool_name: toolName,
    tool_args: {},
    status,
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
    it('contentBlocks: text → tool_call → text 时 fragments 顺序一致', () => {
      const message = createMessage({
        contentBlocks: [
          textBlock('分析中'),
          toolCallBlock('tc-1', 'read_file'),
          textBlock('完成'),
        ],
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      const types = extractFragmentTypes(result.current.fragments)
      expect(types).toEqual(['text', 'tool_call', 'text'])
    })

    it('fragments 内容与 contentBlocks 对应', () => {
      const message = createMessage({
        contentBlocks: [
          textBlock('分析中'),
          toolCallBlock('tc-1', 'read_file'),
          textBlock('完成'),
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
    it('contentBlocks: thinking → text → tool_call → text 时顺序一致', () => {
      const message = createMessage({
        contentBlocks: [
          thinkingBlock('让我想想...'),
          textBlock('使用工具分析'),
          toolCallBlock('tc-1', 'search'),
          textBlock('结果如下'),
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
        contentBlocks: [
          thinkingBlock('分析问题中...'),
          textBlock('结论'),
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
        contentBlocks: [
          toolCallBlock('tc-1', 'read_file'),
          toolCallBlock('tc-2', 'write_file'),
          toolCallBlock('tc-3', 'execute'),
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

    it('tool_call 顺序与 contentBlocks 中出现顺序一致', () => {
      const message = createMessage({
        contentBlocks: [
          textBlock('准备中'),
          toolCallBlock('tc-a', 'tool_a'),
          textBlock('中间'),
          toolCallBlock('tc-b', 'tool_b'),
          textBlock('结束'),
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
  // 4. 动态 contentBlocks 更新
  // -----------------------------------------------------------------------
  describe('动态 contentBlocks 更新', () => {
    it('contentBlocks 增长时 fragments 数量同步增长', () => {
      const initialBlocks: ContentBlock[] = [textBlock('分析中')]

      const { result, rerender } = renderHook(
        ({ blocks }) =>
          useMessageRender({
            message: createMessage({ contentBlocks: blocks }),
          }),
        { initialProps: { blocks: initialBlocks } },
      )

      // 初始 1 个 fragment
      expect(result.current.fragments).toHaveLength(1)
      expect(result.current.fragments[0]).toMatchObject({
        type: 'text',
        content: '分析中',
      })

      // 追加一个 tool_call
      const updatedBlocks: ContentBlock[] = [
        textBlock('分析中'),
        toolCallBlock('tc-1', 'search'),
      ]
      rerender({ blocks: updatedBlocks })

      expect(result.current.fragments).toHaveLength(2)
      expect(result.current.fragments[0]).toMatchObject({ type: 'text' })
      expect(result.current.fragments[1]).toMatchObject({ type: 'tool_call' })
    })

    it('contentBlocks 从 2 个增长到 4 个时顺序不变', () => {
      const { result, rerender } = renderHook(
        ({ blocks }) =>
          useMessageRender({
            message: createMessage({ contentBlocks: blocks }),
          }),
        {
          initialProps: {
            blocks: [
              textBlock('第一步'),
              toolCallBlock('tc-1', 'tool_a'),
            ],
          },
        },
      )

      expect(extractFragmentTypes(result.current.fragments)).toEqual([
        'text',
        'tool_call',
      ])

      rerender({
        blocks: [
          textBlock('第一步'),
          toolCallBlock('tc-1', 'tool_a'),
          textBlock('中间步骤'),
          toolCallBlock('tc-2', 'tool_b'),
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
        contentBlocks: [
          textBlock('第一段'),
          toolCallBlock('tc-1', 'search'),
          textBlock('第二段'),
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
        contentBlocks: [textBlock('内容')],
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
        contentBlocks: [toolCallBlock('tc-1', 'tool')],
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
    it('isStreaming=true 时 contentBlocks 持续追加顺序正确', () => {
      const { result, rerender } = renderHook(
        ({ blocks, isGenerating }) =>
          useMessageRender({
            message: createMessage({
              contentBlocks: blocks,
              role: 'assistant',
            }),
            isLast: true,
            isGenerating,
          }),
        {
          initialProps: {
            blocks: [textBlock('开始')],
            isGenerating: true,
          },
        },
      )

      // 第一轮
      expect(extractFragmentTypes(result.current.fragments)).toEqual(['text'])
      expect(result.current.isStreaming).toBe(true)

      // 第二轮：追加 tool_call
      rerender({
        blocks: [textBlock('开始'), toolCallBlock('tc-1', 'search')],
        isGenerating: true,
      })
      expect(extractFragmentTypes(result.current.fragments)).toEqual([
        'text',
        'tool_call',
      ])

      // 第三轮：追加 text
      rerender({
        blocks: [
          textBlock('开始'),
          toolCallBlock('tc-1', 'search'),
          textBlock('结果'),
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
        blocks: [
          textBlock('开始'),
          toolCallBlock('tc-1', 'search'),
          textBlock('结果'),
        ],
        isGenerating: false,
      })
      expect(result.current.isStreaming).toBe(false)
    })
  })

  // -----------------------------------------------------------------------
  // 7. 空 contentBlocks 降级
  // -----------------------------------------------------------------------
  describe('空 contentBlocks 降级', () => {
    it('无 contentBlocks 时从 content + toolCalls 构建', () => {
      const message = createMessage({
        content: '这是纯文本内容',
        toolCalls: [createToolCall('tc-1', 'search')],
        thinking: { content: '思考中', isThinking: false },
        // 不设置 contentBlocks
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      // buildContentBlocksFromMessage: thinking → toolCalls → content
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

    it('空 contentBlocks 数组也走降级路径', () => {
      const message = createMessage({
        content: '文本',
        contentBlocks: [], // 空数组
      })

      const { result } = renderHook(() =>
        useMessageRender({ message }),
      )

      // buildContentBlocksFromMessage 从 content 构建
      expect(result.current.fragments).toHaveLength(1)
      expect(result.current.fragments[0]).toMatchObject({
        type: 'text',
        content: '文本',
      })
    })

    it('仅有 content 无 toolCalls 时降级为单个 text', () => {
      const message = createMessage({
        content: '纯文本消息',
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

    it('仅有 toolCalls 无 content 时降级为 tool_call fragments', () => {
      const message = createMessage({
        content: '',
        toolCalls: [
          createToolCall('tc-1', 'tool_a'),
          createToolCall('tc-2', 'tool_b'),
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
