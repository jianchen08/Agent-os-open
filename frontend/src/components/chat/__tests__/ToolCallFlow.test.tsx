/**
 * 工具调用流程端到端测试
 *
 * 验证 AC-1h: 工具调用流程完整跑通
 *
 * 测试覆盖：
 * 1. 单工具调用：execution_start → execution_progress → execution_done(success)
 * 2. 多工具调用顺序：execution_start(search) → execution_done → execution_start(analyze) → execution_done
 * 3. 工具调用+文本混合：stream_chunk → execution_start → execution_done → stream_chunk
 * 4. 工具进度显示：execution_start → execution_progress(30%) → execution_progress(80%) → execution_done
 *
 * 测试策略：
 * - 使用 renderHook 测试 useMessageRender hook 的输出
 * - 通过构造不同阶段的 Message 对象（含 contentBlocks）模拟状态转换
 * - 验证 fragments 的 type、index、total 属性
 * - 验证 ActivityCard 的 status 属性变化
 */

import { act } from '@testing-library/react'
import type { ContentBlock, Message, MessageToolCall } from '@/types/models'
import {
  createMockContentBlock,
  createMockMessage,
  createMockToolCall,
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
    progress: toolCall.progress,
    currentStep: toolCall.currentStep,
    durationMs: toolCall.duration_ms,
    error: toolCall.error,
    details: toolCall.result !== undefined
      ? [{ id: 'args', label: '参数', content: toolCall.tool_args ?? {}, contentType: 'json' }]
      : [],
    actions: [],
  }),
  enhanceActivityWithToolConfig: (base: Record<string, unknown>) => base,
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (base: Record<string, unknown>) => base,
}))

// ============================================================
// 辅助函数
// ============================================================

/**
 * 构造包含工具调用的 contentBlocks
 */
function buildToolCallContentBlocks(
  toolCall: MessageToolCall,
  messageId: string,
): ContentBlock[] {
  return [createMockContentBlock('tool_call', { toolCall, sourceId: messageId })]
}

/**
 * 构造文本+工具调用混合的 contentBlocks
 */
function buildMixedContentBlocks(
  items: Array<{ type: 'text'; text: string } | { type: 'tool_call'; toolCall: MessageToolCall }>,
  messageId: string,
): ContentBlock[] {
  return items.map((item) => {
    if (item.type === 'text') {
      return createMockContentBlock('text', { text: item.text, sourceId: messageId })
    }
    return createMockContentBlock('tool_call', {
      toolCall: item.toolCall,
      sourceId: messageId,
    })
  })
}

// ============================================================
// 测试套件
// ============================================================

describe('AC-1h: 工具调用流程', () => {
  // ----------------------------------------------------------
  // 测试 1: 单工具调用
  // ----------------------------------------------------------
  describe('单工具调用', () => {
    it('execution_start → execution_progress → execution_done 应产生 tool_call 类型片段', async () => {
      const messageId = 'msg-tool-1'

      // 模拟 execution_done 后的消息状态
      const toolCall = createMockToolCall({
        call_id: 'exec-search-1',
        tool_name: 'search',
        status: 'completed',
        progress: 100,
        result: { answer: '搜索结果' },
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      // 验证产生 tool_call 类型片段
      expect(result.current.fragments.length).toBe(1)
      expect(result.current.fragments[0].type).toBe('tool_call')

      if (result.current.fragments[0].type === 'tool_call') {
        const fragment = result.current.fragments[0]
        // 验证 index 和 total（单工具调用：index=0, total=1）
        expect(fragment.index).toBe(0)
        expect(fragment.total).toBe(1)

        // 验证工具名称
        expect(fragment.toolCall.tool_name).toBe('search')
        expect(fragment.toolCall.status).toBe('completed')

        // 验证 activity 数据
        expect(fragment.activity.id).toBe('exec-search-1')
        expect(fragment.activity.title).toBe('search')
        expect(fragment.activity.status).toBe('completed')
      }
    })

    it('工具调用运行中状态应为 running', async () => {
      const messageId = 'msg-tool-2'

      const toolCall = createMockToolCall({
        call_id: 'exec-search-2',
        tool_name: 'search',
        status: 'running',
        progress: 50,
        currentStep: '搜索中',
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
        status: 'streaming',
      })

      const { result } = await renderUseMessageRender(message, {
        isLast: true,
        isGenerating: true,
      })

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.status).toBe('running')
        expect(result.current.fragments[0].activity.status).toBe('running')
      }
    })

    it('工具调用初始状态应为 pending', async () => {
      const messageId = 'msg-tool-3'

      const toolCall = createMockToolCall({
        call_id: 'exec-search-3',
        tool_name: 'search',
        status: 'pending',
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.status).toBe('pending')
        expect(result.current.fragments[0].activity.status).toBe('pending')
      }
    })

    it('工具调用失败状态应为 failed', async () => {
      const messageId = 'msg-tool-4'

      const toolCall = createMockToolCall({
        call_id: 'exec-search-4',
        tool_name: 'search',
        status: 'failed',
        error: '连接超时',
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.status).toBe('failed')
        expect(result.current.fragments[0].toolCall.error).toBe('连接超时')
        expect(result.current.fragments[0].activity.status).toBe('failed')
        expect(result.current.fragments[0].activity.error).toBe('连接超时')
      }
    })
  })

  // ----------------------------------------------------------
  // 测试 2: 多工具调用顺序
  // ----------------------------------------------------------
  describe('多工具调用顺序', () => {
    it('两个工具调用应按序渲染，index 和 total 正确', async () => {
      const messageId = 'msg-multi-1'

      const toolCall1 = createMockToolCall({
        call_id: 'exec-search-1',
        tool_name: 'search',
        status: 'completed',
        result: { items: ['结果1', '结果2'] },
      })

      const toolCall2 = createMockToolCall({
        call_id: 'exec-analyze-1',
        tool_name: 'analyze',
        status: 'completed',
        result: { summary: '分析完成' },
      })

      const contentBlocks = buildMixedContentBlocks(
        [
          { type: 'tool_call', toolCall: toolCall1 },
          { type: 'tool_call', toolCall: toolCall2 },
        ],
        messageId,
      )

      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      // 验证两个 tool_call 片段
      expect(result.current.fragments.length).toBe(2)

      // 第一个工具调用
      expect(result.current.fragments[0].type).toBe('tool_call')
      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].index).toBe(0)
        expect(result.current.fragments[0].total).toBe(2)
        expect(result.current.fragments[0].toolCall.tool_name).toBe('search')
        expect(result.current.fragments[0].activity.title).toBe('search')
      }

      // 第二个工具调用
      expect(result.current.fragments[1].type).toBe('tool_call')
      if (result.current.fragments[1].type === 'tool_call') {
        expect(result.current.fragments[1].index).toBe(1)
        expect(result.current.fragments[1].total).toBe(2)
        expect(result.current.fragments[1].toolCall.tool_name).toBe('analyze')
        expect(result.current.fragments[1].activity.title).toBe('analyze')
      }
    })

    it('工具调用的 key 应包含 call_id 以区分不同工具', async () => {
      const messageId = 'msg-multi-2'

      const toolCall1 = createMockToolCall({ call_id: 'call-a', tool_name: 'tool-a' })
      const toolCall2 = createMockToolCall({ call_id: 'call-b', tool_name: 'tool-b' })

      const contentBlocks = buildMixedContentBlocks(
        [
          { type: 'tool_call', toolCall: toolCall1 },
          { type: 'tool_call', toolCall: toolCall2 },
        ],
        messageId,
      )

      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      if (
        result.current.fragments[0].type === 'tool_call' &&
        result.current.fragments[1].type === 'tool_call'
      ) {
        expect(result.current.fragments[0].key).toContain('call-a')
        expect(result.current.fragments[1].key).toContain('call-b')
      }
    })
  })

  // ----------------------------------------------------------
  // 测试 3: 工具调用+文本混合
  // ----------------------------------------------------------
  describe('工具调用+文本混合', () => {
    it('text → tool_call → text 交替顺序应正确', async () => {
      const messageId = 'msg-mixed-1'

      const toolCall = createMockToolCall({
        call_id: 'exec-search-m1',
        tool_name: 'search',
        status: 'completed',
        result: '搜索结果',
      })

      const contentBlocks = buildMixedContentBlocks(
        [
          { type: 'text', text: '使用搜索' },
          { type: 'tool_call', toolCall },
          { type: 'text', text: '根据结果' },
        ],
        messageId,
      )

      const message = createMockMessage({
        id: messageId,
        content: '使用搜索根据结果',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      // 验证交替顺序: text → tool_call → text
      expect(result.current.fragments.length).toBe(3)
      expect(result.current.fragments.map((f) => f.type)).toEqual([
        'text',
        'tool_call',
        'text',
      ])

      // 验证第一个文本
      if (result.current.fragments[0].type === 'text') {
        expect(result.current.fragments[0].content).toBe('使用搜索')
      }

      // 验证工具调用
      if (result.current.fragments[1].type === 'tool_call') {
        expect(result.current.fragments[1].toolCall.tool_name).toBe('search')
        expect(result.current.fragments[1].index).toBe(0)
        expect(result.current.fragments[1].total).toBe(1)
      }

      // 验证第二个文本
      if (result.current.fragments[2].type === 'text') {
        expect(result.current.fragments[2].content).toBe('根据结果')
      }
    })

    it('thinking → tool_call → text 混合应正确', async () => {
      const messageId = 'msg-mixed-2'

      const toolCall = createMockToolCall({
        call_id: 'exec-tool-m2',
        tool_name: 'analyze',
        status: 'completed',
      })

      const contentBlocks: ContentBlock[] = [
        createMockContentBlock('thinking', {
          thinking: { content: '需要分析数据', isThinking: false },
          sourceId: messageId,
        }),
        createMockContentBlock('tool_call', { toolCall, sourceId: messageId }),
        createMockContentBlock('text', { text: '分析完成', sourceId: messageId }),
      ]

      const message = createMockMessage({
        id: messageId,
        content: '分析完成',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      expect(result.current.fragments.map((f) => f.type)).toEqual([
        'thinking',
        'tool_call',
        'text',
      ])
    })

    it('多个工具调用穿插文本应正确', async () => {
      const messageId = 'msg-mixed-3'

      const toolCall1 = createMockToolCall({
        call_id: 'call-1',
        tool_name: 'search',
        status: 'completed',
      })
      const toolCall2 = createMockToolCall({
        call_id: 'call-2',
        tool_name: 'translate',
        status: 'completed',
      })

      const contentBlocks = buildMixedContentBlocks(
        [
          { type: 'text', text: '开始' },
          { type: 'tool_call', toolCall: toolCall1 },
          { type: 'text', text: '中间' },
          { type: 'tool_call', toolCall: toolCall2 },
          { type: 'text', text: '结束' },
        ],
        messageId,
      )

      const message = createMockMessage({
        id: messageId,
        content: '开始中间结束',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      expect(result.current.fragments.map((f) => f.type)).toEqual([
        'text',
        'tool_call',
        'text',
        'tool_call',
        'text',
      ])

      // 验证两个 tool_call 的 index/total
      const toolFragments = result.current.fragments.filter((f) => f.type === 'tool_call')
      expect(toolFragments.length).toBe(2)
      if (toolFragments[0].type === 'tool_call') {
        expect(toolFragments[0].index).toBe(0)
        expect(toolFragments[0].total).toBe(2)
      }
      if (toolFragments[1].type === 'tool_call') {
        expect(toolFragments[1].index).toBe(1)
        expect(toolFragments[1].total).toBe(2)
      }
    })
  })

  // ----------------------------------------------------------
  // 测试 4: 工具进度显示
  // ----------------------------------------------------------
  describe('工具进度显示', () => {
    it('进度应随 execution_progress 事件更新', async () => {
      const messageId = 'msg-progress-1'

      // 模拟进度 30% 的状态
      const toolCall30 = createMockToolCall({
        call_id: 'exec-progress-1',
        tool_name: 'search',
        status: 'running',
        progress: 30,
        currentStep: '搜索中',
      })

      const contentBlocks30 = buildToolCallContentBlocks(toolCall30, messageId)
      const message30 = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks: contentBlocks30,
        status: 'streaming',
      })

      const { result, rerender } = await renderUseMessageRender(message30, {
        isLast: true,
        isGenerating: true,
      })

      // 验证 30% 进度
      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.progress).toBe(30)
        expect(result.current.fragments[0].activity.progress).toBe(30)
        expect(result.current.fragments[0].activity.currentStep).toBe('搜索中')
      }

      // 更新到 80% 进度
      const toolCall80 = createMockToolCall({
        call_id: 'exec-progress-1',
        tool_name: 'search',
        status: 'running',
        progress: 80,
        currentStep: '分析中',
      })

      const contentBlocks80 = buildToolCallContentBlocks(toolCall80, messageId)
      const message80 = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks: contentBlocks80,
        status: 'streaming',
      })

      await act(async () => {
        rerender({
          message: message80,
          isLast: true,
          isGenerating: true,
        })
      })

      // 验证 80% 进度
      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.progress).toBe(80)
        expect(result.current.fragments[0].activity.progress).toBe(80)
        expect(result.current.fragments[0].activity.currentStep).toBe('分析中')
      }
    })

    it('进度完成后 progress 应为 100 且 status 为 completed', async () => {
      const messageId = 'msg-progress-2'

      const toolCall = createMockToolCall({
        call_id: 'exec-progress-2',
        tool_name: 'search',
        status: 'completed',
        progress: 100,
        result: { found: true },
        duration_ms: 1500,
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.status).toBe('completed')
        expect(result.current.fragments[0].toolCall.progress).toBe(100)
        expect(result.current.fragments[0].activity.status).toBe('completed')
        expect(result.current.fragments[0].activity.progress).toBe(100)
      }
    })

    it('完整进度转换: 0% → 30% → 80% → 100%', async () => {
      const messageId = 'msg-progress-3'

      // 0% - 初始执行
      const toolCall0 = createMockToolCall({
        call_id: 'exec-progress-3',
        tool_name: 'search',
        status: 'running',
        progress: 0,
      })

      const contentBlocks0 = buildToolCallContentBlocks(toolCall0, messageId)
      const message0 = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks: contentBlocks0,
        status: 'streaming',
      })

      const { result, rerender } = await renderUseMessageRender(message0, {
        isLast: true,
        isGenerating: true,
      })

      // 初始：progress=0
      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].activity.progress).toBe(0)
        expect(result.current.fragments[0].activity.status).toBe('running')
      }

      // 更新到 30%
      const toolCall30 = createMockToolCall({
        call_id: 'exec-progress-3',
        tool_name: 'search',
        status: 'running',
        progress: 30,
        currentStep: '搜索中',
      })
      const message30 = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks: buildToolCallContentBlocks(toolCall30, messageId),
        status: 'streaming',
      })

      await act(async () => {
        rerender({ message: message30, isLast: true, isGenerating: true })
      })

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].activity.progress).toBe(30)
      }

      // 更新到 80%
      const toolCall80 = createMockToolCall({
        call_id: 'exec-progress-3',
        tool_name: 'search',
        status: 'running',
        progress: 80,
        currentStep: '分析中',
      })
      const message80 = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks: buildToolCallContentBlocks(toolCall80, messageId),
        status: 'streaming',
      })

      await act(async () => {
        rerender({ message: message80, isLast: true, isGenerating: true })
      })

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].activity.progress).toBe(80)
      }

      // 完成 100%
      const toolCall100 = createMockToolCall({
        call_id: 'exec-progress-3',
        tool_name: 'search',
        status: 'completed',
        progress: 100,
        result: { done: true },
      })
      const message100 = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks: buildToolCallContentBlocks(toolCall100, messageId),
        status: 'completed',
      })

      await act(async () => {
        rerender({ message: message100, isLast: true, isGenerating: false })
      })

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].activity.progress).toBe(100)
        expect(result.current.fragments[0].activity.status).toBe('completed')
      }
      expect(result.current.isStreaming).toBe(false)
    })
  })

  // ----------------------------------------------------------
  // 测试 5: 工具调用取消
  // ----------------------------------------------------------
  describe('工具调用取消', () => {
    it('execution_cancelled 应导致 cancelled 状态', async () => {
      const messageId = 'msg-cancel-1'

      const toolCall = createMockToolCall({
        call_id: 'exec-cancel-1',
        tool_name: 'long_task',
        status: 'cancelled' as MessageToolCall['status'],
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.status).toBe('cancelled')
        expect(result.current.fragments[0].activity.status).toBe('cancelled')
      }
    })
  })

  // ----------------------------------------------------------
  // 测试 6: 详情内容验证
  // ----------------------------------------------------------
  describe('详情内容验证', () => {
    it('tool_call 片段的 activity 应包含工具名和结果', async () => {
      const messageId = 'msg-detail-1'

      const toolCall = createMockToolCall({
        call_id: 'exec-detail-1',
        tool_name: 'web_search',
        tool_args: { query: 'React Testing Library' },
        status: 'completed',
        result: { items: ['结果1', '结果2'] },
        duration_ms: 2300,
      })

      const contentBlocks = buildToolCallContentBlocks(toolCall, messageId)
      const message = createMockMessage({
        id: messageId,
        content: '',
        contentBlocks,
      })

      const { result } = await renderUseMessageRender(message)

      if (result.current.fragments[0].type === 'tool_call') {
        const fragment = result.current.fragments[0]
        // 验证工具名称
        expect(fragment.activity.title).toBe('web_search')
        expect(fragment.activity.toolName).toBe('web_search')

        // 验证状态
        expect(fragment.activity.status).toBe('completed')

        // 验证时长
        expect(fragment.activity.durationMs).toBe(2300)
      }
    })
  })
})
