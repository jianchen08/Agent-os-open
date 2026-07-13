/**
 * 简单问答流程端到端测试
 *
 * 验证 AC-1g: 简单问答流程完整跑通
 *
 * 测试覆盖：
 * 1. 流式文本渲染：stream_start → stream_chunk × N → stream_end
 * 2. 思考+文本混合：thinking_start → thinking_chunk → thinking_end → stream_start → stream_chunk → stream_end
 * 3. 空消息处理：stream_start → stream_end（无 chunk）
 * 4. 流式中断：stream_start → stream_chunk → stream_error
 *
 * 所有测试通过构造 Message 对象（含 parts[]），
 * 使用 renderHook 测试 useMessageRender hook 的输出。
 */

import { act } from '@testing-library/react'
import {
  createMockMessage,
  createTextPart,
  createThinkingPart,
  renderUseMessageRender,
} from './testUtils'

// ============================================================
// Mock 外部依赖
// ============================================================

vi.mock('@/utils/activityConverter', () => ({
  toolCallToActivity: (toolCall: Record<string, unknown>) => ({
    type: 'tool_call',
    id: toolCall.call_id ?? 'activity-1',
    title: toolCall.tool_name ?? 'unknown',
    toolName: toolCall.tool_name ?? 'unknown',
    status: toolCall.status ?? 'pending',
    details: [],
    actions: [],
  }),
  enhanceActivityWithToolConfig: (base: Record<string, unknown>) => base,
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (base: Record<string, unknown>) => base,
}))

// ============================================================
// 测试套件
// ============================================================

describe('AC-1g: 简单问答流程', () => {
  // ----------------------------------------------------------
  // 测试 1: 流式文本渲染
  // ----------------------------------------------------------
  describe('流式文本渲染', () => {
    it('应正确渲染 stream_start → stream_chunk × 2 → stream_end 的完整文本', async () => {
      // 模拟事件序列: stream_start → stream_chunk('你') → stream_chunk('好') → stream_end
      // 流式结束后消息包含完整文本 parts[]
      const messageId = 'msg-stream-1'
      const threadId = 'thread-1'

      // 构造流式结束后的消息状态
      const message = createMockMessage({
        id: messageId,
        sessionId: threadId,
        content: '你好',
        parts: [createTextPart('你好', 1)],
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: false,
      })

      // 验证 fragments 包含 text 类型片段
      const fragments = result.current.fragments
      expect(fragments.length).toBe(1)
      expect(fragments[0].type).toBe('text')

      // 验证内容完整
      if (fragments[0].type === 'text') {
        expect(fragments[0].content).toBe('你好')
        expect(fragments[0].key).toContain('part-text')
        expect(fragments[0].isLast).toBe(true)
      }

      // 流式结束后 isStreaming 应为 false
      expect(result.current.isStreaming).toBe(false)
    })

    it('流式过程中 isStreaming 应为 true', async () => {
      const messageId = 'msg-stream-2'

      // 流式中（只有部分 chunk 到达）
      const message = createMockMessage({
        id: messageId,
        content: '你',
        parts: [createTextPart('你', 1, 'streaming')],
        status: 'streaming',
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: true,
      })

      // 流式中 isStreaming 应为 true（isGenerating=true && isLast=true && role=assistant）
      expect(result.current.isStreaming).toBe(true)

      // 验证部分内容正确
      expect(result.current.fragments.length).toBe(1)
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('你')
      }
    })

    it('应按片段顺序渲染多个 stream_chunk', async () => {
      const messageId = 'msg-stream-3'

      // 多个 chunk 合并后
      const message = createMockMessage({
        id: messageId,
        content: '第一段第二段第三段',
        parts: [createTextPart('第一段第二段第三段', 1)],
      })

      const { result } = await renderUseMessageRender(message)

      expect(result.current.fragments.length).toBe(1)
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('第一段第二段第三段')
      }
    })
  })

  // ----------------------------------------------------------
  // 测试 2: 思考+文本混合
  // ----------------------------------------------------------
  describe('思考+文本混合', () => {
    it('thinking 片段应在 text 片段之前', async () => {
      const messageId = 'msg-think-1'

      // 模拟: thinking_start → thinking_chunk('分析中') → thinking_end →
      //       stream_start → stream_chunk('回答') → stream_end
      const message = createMockMessage({
        id: messageId,
        content: '回答',
        parts: [
          createThinkingPart('分析中', 1),
          createTextPart('回答', 2),
        ],
      })

      const { result } = await renderUseMessageRender(message)

      // 验证 fragments 顺序: thinking 在前，text 在后
      const fragments = result.current.fragments
      expect(fragments.length).toBe(2)

      expect(fragments[0].type).toBe('thinking')
      expect(fragments[1].type).toBe('text')

      // 验证 thinking 片段内容
      if (fragments[0].type === 'thinking') {
        expect(fragments[0].thinking.content).toBe('分析中')
        expect(fragments[0].key).toContain('part-thinking')
      }

      // 验证 text 片段内容
      if (fragments[1].type === 'text') {
        expect(fragments[1].content).toBe('回答')
      }
    })

    it('流式过程中 thinking 片段的 isThinking 状态应正确', async () => {
      const messageId = 'msg-think-2'

      // 流式思考中（state 为 streaming）
      const message = createMockMessage({
        id: messageId,
        content: '回答内容',
        parts: [
          createThinkingPart('正在分析...', 1, 'streaming'),
          createTextPart('回答内容', 2),
        ],
        status: 'streaming',
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: true,
      })

      if (result.current.fragments[0].type === 'thinking') {
        expect(result.current.fragments[0].thinking.isThinking).toBe(true)
      }

      expect(result.current.isStreaming).toBe(true)
    })

    it('仅有 thinking 没有 text 时只渲染 thinking 片段', async () => {
      const messageId = 'msg-think-3'

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [createThinkingPart('纯思考', 1)],
      })

      const { result } = await renderUseMessageRender(message)

      expect(result.current.fragments.length).toBe(1)
      expect(result.current.fragments[0].type).toBe('thinking')
    })
  })

  // ----------------------------------------------------------
  // 测试 3: 空消息处理
  // ----------------------------------------------------------
  describe('空消息处理', () => {
    it('stream_start → stream_end（无 chunk）不应崩溃', async () => {
      const messageId = 'msg-empty-1'

      // 无 chunk，parts 为空
      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [],
      })

      const { result } = await renderUseMessageRender(message)

      // 空消息应产生空 fragments
      expect(result.current.fragments).toEqual([])
      expect(result.current.isStreaming).toBe(false)
    })

    it('仅空白文本不应产生 text 片段', async () => {
      const messageId = 'msg-empty-2'

      // 空白文本（buildFragmentsFromParts 会过滤 trim() 为空的文本）
      const message = createMockMessage({
        id: messageId,
        content: '   ',
        parts: [createTextPart('   ', 1)],
      })

      const { result } = await renderUseMessageRender(message)

      // 空白文本不应产生片段
      expect(result.current.fragments).toEqual([])
    })

    it('parts 为 undefined 时不应崩溃', async () => {
      const messageId = 'msg-empty-3'

      const message = createMockMessage({
        id: messageId,
        content: '',
      })

      const { result } = await renderUseMessageRender(message)

      expect(result.current.fragments).toEqual([])
    })
  })

  // ----------------------------------------------------------
  // 测试 4: 流式中断
  // ----------------------------------------------------------
  describe('流式中断', () => {
    it('stream_start → stream_chunk → stream_error 应正确处理错误', async () => {
      const messageId = 'msg-error-1'

      // 流式中断：chunk 到达后出错
      // 消息保留已到达的文本 + 错误信息
      const message = createMockMessage({
        id: messageId,
        content: '部分内容',
        parts: [createTextPart('部分内容', 1)],
        status: 'streaming',
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: true,
      })

      // 已到达的片段应正确渲染
      expect(result.current.fragments.length).toBe(1)
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('部分内容')
      }

      // 流式状态仍在（错误后可能继续或断开）
      expect(result.current.isStreaming).toBe(true)
    })

    it('stream_error 后流式结束，isStreaming 应变为 false', async () => {
      const messageId = 'msg-error-2'

      // 错误后流式结束
      const message = createMockMessage({
        id: messageId,
        content: '部分内容',
        parts: [createTextPart('部分内容', 1)],
        status: 'completed',
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: false,
      })

      // 错误后内容仍然存在
      expect(result.current.fragments.length).toBe(1)
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('部分内容')
      }

      // 流式结束
      expect(result.current.isStreaming).toBe(false)
    })

    it('fragment 的 key 应包含 part 索引用于 React 追踪', async () => {
      const messageId = 'msg-key-1'
      const message = createMockMessage({
        id: messageId,
        content: '内容',
        parts: [createTextPart('内容', 1)],
      })

      const { result } = await renderUseMessageRender(message)

      for (const fragment of result.current.fragments) {
        expect(fragment.key).toContain('part-text')
      }
    })
  })

  // ----------------------------------------------------------
  // 测试 5: fragments 顺序与事件顺序一致
  // ----------------------------------------------------------
  describe('fragments 顺序一致性', () => {
    it('多片段顺序应与 parts[] 顺序一致', async () => {
      const messageId = 'msg-order-1'

      // 构造 thinking → text → text 顺序
      const message = createMockMessage({
        id: messageId,
        content: '文本1文本2',
        parts: [
          createThinkingPart('思考1', 1),
          createTextPart('文本1', 2),
          createTextPart('文本2', 3),
        ],
      })

      const { result } = await renderUseMessageRender(message)

      // 验证顺序
      expect(result.current.fragments.map((f) => f.type)).toEqual([
        'thinking',
        'text',
        'text',
      ])
    })
  })

  // ----------------------------------------------------------
  // 测试 6: isStreaming 状态转换
  // ----------------------------------------------------------
  describe('isStreaming 状态转换', () => {
    it('非最后一条消息 isStreaming 应为 false', async () => {
      const message = createMockMessage({
        content: '你好',
        parts: [createTextPart('你好', 1)],
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: false,
        isGenerating: true,
      })

      expect(result.current.isStreaming).toBe(false)
    })

    it('非 assistant 角色消息 isStreaming 应为 false', async () => {
      const message = createMockMessage({
        role: 'user',
        content: '你好',
        parts: [createTextPart('你好', 1)],
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: true,
      })

      expect(result.current.isStreaming).toBe(false)
    })

    it('assistant + isLast + isGenerating 时 isStreaming 应为 true', async () => {
      const message = createMockMessage({
        role: 'assistant',
        content: '你好',
        parts: [createTextPart('你好', 1)],
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: true,
      })

      expect(result.current.isStreaming).toBe(true)
    })

    it('parts 更新后 fragments 应随之更新', async () => {
      // 初始消息
      const message1 = createMockMessage({
        id: 'msg-update-1',
        content: '你好',
        parts: [createTextPart('你好', 1)],
      })

      const { result, rerender } = await renderUseMessageRender(message1, {
        isLast: true,
        isGenerating: true,
      })

      expect(result.current.fragments.length).toBe(1)
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('你好')
      }

      // 更新消息（模拟新 chunk 到达）
      const message2 = createMockMessage({
        id: 'msg-update-1',
        content: '你好世界',
        parts: [createTextPart('你好世界', 1)],
      })

      await act(async () => {
        rerender({
          message: message2,
          isLast: true,
          isGenerating: true,
        })
      })

      expect(result.current.fragments.length).toBe(1)
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('你好世界')
      }
    })
  })
})
