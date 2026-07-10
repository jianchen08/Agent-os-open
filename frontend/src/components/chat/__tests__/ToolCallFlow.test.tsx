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
 * - 通过构造不同阶段的 Message 对象（含 parts[]）模拟状态转换
 * - 验证 fragments 的 type、index、total 属性
 * - 验证 ActivityCard 的 status 属性变化
 */

import { act } from '@testing-library/react'
import {
  createMockMessage,
  createToolCallPart,
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
// 测试套件
// ============================================================

describe('AC-1h: 工具调用流程', () => {
  // ----------------------------------------------------------
  // 测试 1: 单工具调用
  // ----------------------------------------------------------
  describe('单工具调用', () => {
    it('execution_start → execution_progress → execution_done 应产生 tool_call 类型片段', async () => {
      const messageId = 'msg-tool-1'

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-search-1',
            name: 'search',
            state: 'done',
            progress: 100,
            result: { answer: '搜索结果' },
            sequence: 1,
          }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-search-2',
            name: 'search',
            state: 'calling',
            progress: 50,
            currentStep: '搜索中',
            sequence: 1,
          }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-search-3',
            name: 'search',
            state: 'streaming',
            sequence: 1,
          }),
        ],
      })

      const { result } = await renderUseMessageRender(message)

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].toolCall.status).toBe('pending')
        expect(result.current.fragments[0].activity.status).toBe('pending')
      }
    })

    it('工具调用失败状态应为 failed', async () => {
      const messageId = 'msg-tool-4'

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-search-4',
            name: 'search',
            state: 'error',
            error: '连接超时',
            sequence: 1,
          }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-search-1',
            name: 'search',
            state: 'done',
            result: { items: ['结果1', '结果2'] },
            sequence: 1,
          }),
          createToolCallPart({
            callId: 'exec-analyze-1',
            name: 'analyze',
            state: 'done',
            result: { summary: '分析完成' },
            sequence: 2,
          }),
        ],
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

    it('工具调用的 key 应包含 callId 以区分不同工具', async () => {
      const messageId = 'msg-multi-2'

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({ callId: 'call-a', name: 'tool-a', sequence: 1 }),
          createToolCallPart({ callId: 'call-b', name: 'tool-b', sequence: 2 }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '使用搜索根据结果',
        parts: [
          createTextPart('使用搜索', 1),
          createToolCallPart({
            callId: 'exec-search-m1',
            name: 'search',
            state: 'done',
            result: '搜索结果',
            sequence: 2,
          }),
          createTextPart('根据结果', 3),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '分析完成',
        parts: [
          createThinkingPart('需要分析数据', 1),
          createToolCallPart({
            callId: 'exec-tool-m2',
            name: 'analyze',
            state: 'done',
            sequence: 2,
          }),
          createTextPart('分析完成', 3),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '开始中间结束',
        parts: [
          createTextPart('开始', 1),
          createToolCallPart({
            callId: 'call-1',
            name: 'search',
            state: 'done',
            sequence: 2,
          }),
          createTextPart('中间', 3),
          createToolCallPart({
            callId: 'call-2',
            name: 'translate',
            state: 'done',
            sequence: 4,
          }),
          createTextPart('结束', 5),
        ],
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
      const message30 = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-1',
            name: 'search',
            state: 'calling',
            progress: 30,
            currentStep: '搜索中',
            sequence: 1,
          }),
        ],
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
      const message80 = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-1',
            name: 'search',
            state: 'calling',
            progress: 80,
            currentStep: '分析中',
            sequence: 1,
          }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-2',
            name: 'search',
            state: 'done',
            progress: 100,
            result: { found: true },
            durationMs: 1500,
            sequence: 1,
          }),
        ],
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
      const message0 = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-3',
            name: 'search',
            state: 'calling',
            progress: 0,
            sequence: 1,
          }),
        ],
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
      const message30 = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-3',
            name: 'search',
            state: 'calling',
            progress: 30,
            currentStep: '搜索中',
            sequence: 1,
          }),
        ],
        status: 'streaming',
      })

      await act(async () => {
        rerender({ message: message30, isLast: true, isGenerating: true })
      })

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].activity.progress).toBe(30)
      }

      // 更新到 80%
      const message80 = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-3',
            name: 'search',
            state: 'calling',
            progress: 80,
            currentStep: '分析中',
            sequence: 1,
          }),
        ],
        status: 'streaming',
      })

      await act(async () => {
        rerender({ message: message80, isLast: true, isGenerating: true })
      })

      if (result.current.fragments[0].type === 'tool_call') {
        expect(result.current.fragments[0].activity.progress).toBe(80)
      }

      // 完成 100%
      const message100 = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-progress-3',
            name: 'search',
            state: 'done',
            progress: 100,
            result: { done: true },
            sequence: 1,
          }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-cancel-1',
            name: 'long_task',
            state: 'cancelled',
            sequence: 1,
          }),
        ],
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

      const message = createMockMessage({
        id: messageId,
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-detail-1',
            name: 'web_search',
            args: { query: 'React Testing Library' },
            state: 'done',
            result: { items: ['结果1', '结果2'] },
            durationMs: 2300,
            sequence: 1,
          }),
        ],
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
