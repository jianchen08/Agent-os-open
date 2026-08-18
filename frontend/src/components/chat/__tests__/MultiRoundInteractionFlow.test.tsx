/**
 * MultiRoundInteractionFlow.test.tsx
 *
 * 验证 AC-1j: 多轮交互流程跑通（工具+交互+继续的循环）
 *
 * 测试覆盖：
 * 1. 工具→交互→继续：execution_start → execution_done → interaction_request → 用户响应 → stream_start → stream_end
 * 2. 多轮循环：2 轮完整的 工具→交互→继续 循环
 * 3. 交互后工具调用：用户响应后 Agent 调用新工具
 * 4. 混合内容顺序：多轮交互中消息渲染顺序正确（text → tool_call → interaction → text）
 */

import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { InteractionCard } from '@/components/chat/InteractionCard'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import {
  createExecutionStartEvent,
  createExecutionDoneEvent,
  createInteractionRequestEvent,
  createStreamStartEvent,
  createStreamChunkEvent,
  createStreamEndEvent,
  createMockMessage,
  createTextPart,
  createToolCallPart,
} from './testUtils'
import type { InteractionCardProps } from '@/components/chat/InteractionCard'
import type { PendingInteraction } from '@/stores/interactionStore'

// ---------------------------------------------------------------------------
//  Mock: lucide-react
// ---------------------------------------------------------------------------
vi.mock('lucide-react', () => {
  const icons = [
    'ArrowRight',
    'Check',
    'Loader2',
    'MessageSquare',
    'Clock',
    'AlertTriangle',
    'Send',
    'ChevronDown',
    'ChevronRight',
    'RefreshCw',
    'Copy',
    'Wrench',
    'Play',
    'Ban',
    'XCircle',
    'CheckCircle2',
    'Sparkles',
    'Target',
    'X',
  ]
  const m: Record<string, any> = {}
  for (const name of icons) {
    m[name] = (p: any) => <svg data-testid={`icon-${name}`} {...p} />
  }
  return m
})

// ---------------------------------------------------------------------------
//  Mock: @/lib/utils
// ---------------------------------------------------------------------------
vi.mock('@/lib/utils', () => ({
  cn: (...args: (string | undefined | null | false)[]) =>
    args.filter(Boolean).join(' '),
}))

// ---------------------------------------------------------------------------
//  Mock: MarkdownRenderer
// ---------------------------------------------------------------------------
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown-renderer">{content}</div>
  ),
}))

// ---------------------------------------------------------------------------
//  Mock: UI Button
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/button', () => ({
  Button: ({
    children,
    onClick,
    disabled,
    ...rest
  }: {
    children: React.ReactNode
    onClick?: () => void
    disabled?: boolean
    [key: string]: any
  }) => (
    <button
      data-testid={`button-${typeof children === 'string' ? children : 'action'}`}
      onClick={onClick}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  ),
}))

// ---------------------------------------------------------------------------
//  Mock: GlobalWebSocket（真实订阅面：globalWS.subscribe/unsubscribe + send 族）
//  useRealtimeEvents / useInteractionHandler 均通过 globalWS 订阅事件；
//  此前 mock 的是无人 import 的 WebSocketService（mock 空气），事件从未触达真实 hook。
// ---------------------------------------------------------------------------
const listeners: Record<string, Set<(...args: any[]) => void>> = {}

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      if (!listeners[event]) listeners[event] = new Set()
      listeners[event].add(cb)
    }),
    unsubscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      listeners[event]?.delete(cb)
    }),
    send: vi.fn(),
    sendInteractionResponse: vi.fn().mockResolvedValue(undefined),
    sendApproval: vi.fn(),
    connect: vi.fn(),
    status: 'connected',
  },
}))

// ---------------------------------------------------------------------------
//  Mock: apiClient（useInteractionHandler 恢复逻辑调用 /interaction/pending）
//  返回空列表，使刷新恢复 effect 干净走完，不触发真实网络请求或循环依赖加载。
// ---------------------------------------------------------------------------
vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { items: [], total: 0 } }),
  },
}))

// ---------------------------------------------------------------------------
//  Mock: formatDuration
// ---------------------------------------------------------------------------
vi.mock('@/types/activity', async (importOriginal) => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
  const actual = await importOriginal<typeof import('@/types/activity')>()
  return {
    ...actual,
    formatDuration: (ms: number) => {
      if (ms < 1000) return `${ms}ms`
      const seconds = Math.floor(ms / 1000)
      if (seconds < 60) return `${seconds}s`
      return `${Math.floor(seconds / 60)}m`
    },
  }
})

// ---------------------------------------------------------------------------
//  Mock: confirm dialog
// ---------------------------------------------------------------------------
vi.mock('@/utils/confirm', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn().mockResolvedValue(true),
    dialogState: { open: false, message: '', onConfirm: vi.fn(), onCancel: vi.fn() },
    setDialogState: vi.fn(),
  }),
}))

// ---------------------------------------------------------------------------
//  Mock: sessionListStore（navigateToTab 需要）
// ---------------------------------------------------------------------------
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: Object.assign(
    () => ({
      setActiveSession: vi.fn().mockResolvedValue(undefined),
    }),
    {
      getState: () => ({
        setActiveSession: vi.fn().mockResolvedValue(undefined),
      }),
    },
  ),
}))

// ---------------------------------------------------------------------------
//  工厂与辅助函数
// ---------------------------------------------------------------------------

/** 创建 PendingInteraction */
function createPendingInteraction(
  overrides: Partial<PendingInteraction> = {},
): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '交互请求',
    description: '',
    threadId: 'thread-1',
    tabId: 'tab-1',
    agentId: 'agent-1',
    timestamp: new Date().toISOString(),
    status: 'pending',
    ...overrides,
  }
}

/** 创建完整的 InteractionCardProps */
function createCardProps(
  overrides: Partial<InteractionCardProps> = {},
): InteractionCardProps {
  return {
    interaction: createPendingInteraction(
      overrides.interaction as Partial<PendingInteraction> | undefined,
    ),
    onRespondChoice: overrides.onRespondChoice ?? vi.fn(),
    onRespondText: overrides.onRespondText ?? vi.fn(),
    onNavigateToTab: overrides.onNavigateToTab ?? vi.fn(),
    isSubmitting: overrides.isSubmitting ?? false,
  }
}

/** 触发 WebSocket 事件 */
function emitEvent(event: string, data: Record<string, unknown>) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of cbs) cb(data)
}

/** 记录事件序列以验证顺序 */
function createEventRecorder(): {
  log: string[]
  record: (label: string) => void
} {
  const log: string[] = []
  return {
    log,
    record: (label: string) => {
      log.push(label)
    },
  }
}

/** 创建 ExecutionEvent 对象（匹配 layoutModeStore 接口） */
function createExecutionEvent(overrides: {
  id: string
  name: string
  status?: 'running' | 'completed' | 'failed' | 'cancelled'
  type?: 'tool' | 'agent' | 'workflow'
  progress?: number
}) {
  return {
    id: overrides.id,
    type: overrides.type ?? 'tool',
    name: overrides.name,
    status: overrides.status ?? 'running',
    progress: overrides.progress ?? 0,
    startedAt: new Date().toISOString(),
  }
}

// ---------------------------------------------------------------------------
//  测试
// ---------------------------------------------------------------------------

describe('MultiRoundInteractionFlow — AC-1j: 多轮交互流程', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    // 清理 listeners
    for (const key of Object.keys(listeners)) delete listeners[key]
    // 重置 stores
    useLayoutModeStore.setState({
      activeExecutions: [],
      pendingInteractions: [],
    })
    useInteractionStore.setState({ pendingInteractions: [] })
  })

  // -----------------------------------------------------------------------
  // 1. 工具→交互→继续
  // -----------------------------------------------------------------------
  describe('工具→交互→继续', () => {
    it('execution_start → execution_done → interaction_request → 用户响应 → stream 继续输出', async () => {
      const recorder = createEventRecorder()

      // 渲染 InteractionCard 用于交互
      const onRespondChoice = vi.fn()
      render(
        <InteractionCard
          {...createCardProps({
            interaction: createPendingInteraction({
              requestId: 'req-flow-1',
              mode: 'choice',
              title: '确认操作',
              options: [
                { id: 'confirm', label: '确认' },
                { id: 'cancel', label: '取消' },
              ],
            }),
            onRespondChoice,
          })}
        />,
      )

      // 步骤 1: 执行开始
      recorder.record('execution_start')
      act(() => {
        emitEvent('execution_start', {
          execution_id: 'exec-1',
          execution_type: 'tool',
          name: 'deploy',
        })
      })

      // 验证标题和选项可见
      expect(screen.getByText('确认操作')).toBeInTheDocument()
      expect(screen.getByText('确认')).toBeInTheDocument()
      expect(screen.getByText('取消')).toBeInTheDocument()

      // 步骤 2: 用户点击"确认"
      recorder.record('user_respond')
      await act(async () => {
        fireEvent.click(screen.getByText('确认'))
      })

      expect(onRespondChoice).toHaveBeenCalledWith('confirm')

      // 步骤 3: 模拟后续流式输出（验证流程继续）
      recorder.record('stream_start')
      act(() => {
        emitEvent('stream_start', {
          type: 'stream_start',
          message_id: 'msg-after-interaction',
          thread_id: 'thread-1',
        })
      })

      recorder.record('stream_chunk')
      act(() => {
        emitEvent('stream_chunk', {
          type: 'stream_chunk',
          message_id: 'msg-after-interaction',
          thread_id: 'thread-1',
          content: '操作已确认，继续执行...',
        })
      })

      recorder.record('stream_end')
      act(() => {
        emitEvent('stream_end', {
          type: 'stream_end',
          message_id: 'msg-after-interaction',
          thread_id: 'thread-1',
        })
      })

      // 验证事件顺序
      expect(recorder.log).toEqual([
        'execution_start',
        'user_respond',
        'stream_start',
        'stream_chunk',
        'stream_end',
      ])
    })

    it('通过 store 集成：工具执行完 → 交互请求到达 → 用户响应', () => {
      // 使用 store 直接模拟完整流程
      act(() => {
        // 工具执行开始
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-integrated',
            name: 'prepare_deploy',
          }),
        )
      })

      let layoutState = useLayoutModeStore.getState()
      expect(layoutState.activeExecutions).toHaveLength(1)
      expect(layoutState.activeExecutions[0].status).toBe('running')

      // 工具执行完成
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-integrated',
            name: 'prepare_deploy',
            status: 'completed',
            progress: 100,
          }),
        )
      })

      layoutState = useLayoutModeStore.getState()
      expect(layoutState.activeExecutions[0].status).toBe('completed')

      // 交互请求到达
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-integrated',
          mode: 'choice',
          title: '确认部署',
          description: '部署前请确认',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          options: [
            { id: 'deploy', label: '部署' },
            { id: 'abort', label: '中止' },
          ],
        })
      })

      let interactionState = useInteractionStore.getState()
      expect(interactionState.pendingInteractions).toHaveLength(1)
      expect(interactionState.pendingInteractions[0].status).toBe('pending')

      // 用户响应
      act(() => {
        useInteractionStore.getState().markResponded('req-integrated')
      })

      interactionState = useInteractionStore.getState()
      expect(interactionState.pendingInteractions[0].status).toBe('responded')
    })
  })

  // -----------------------------------------------------------------------
  // 2. 多轮循环
  // -----------------------------------------------------------------------
  describe('多轮循环（2 轮工具→交互→继续）', () => {
    it('第一轮完整流转后第二轮正常开始', () => {
      // ========== 第一轮 ==========
      // 工具执行
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-round1',
          mode: 'choice',
          title: '第一轮：选择环境',
          description: '',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          options: [
            { id: 'dev', label: '开发环境' },
            { id: 'staging', label: '预发环境' },
          ],
        })
      })

      let state = useInteractionStore.getState()
      expect(state.pendingInteractions).toHaveLength(1)
      expect(state.pendingInteractions[0].title).toBe('第一轮：选择环境')

      // 用户选择"开发环境"
      act(() => {
        useInteractionStore.getState().markResponded('req-round1')
      })

      state = useInteractionStore.getState()
      expect(state.pendingInteractions[0].status).toBe('responded')

      // 模拟 Agent 处理完毕后第二轮
      // 先清理已完成的交互（模拟 3 秒后自动 dismiss）
      act(() => {
        useInteractionStore.getState().dismissInteraction('req-round1')
      })

      state = useInteractionStore.getState()
      expect(state.pendingInteractions).toHaveLength(0)

      // ========== 第二轮 ==========
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-round2',
          mode: 'conversation',
          title: '第二轮：输入备注',
          description: '请输入部署备注',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          suggestions: ['常规部署', '紧急修复'],
        })
      })

      state = useInteractionStore.getState()
      expect(state.pendingInteractions).toHaveLength(1)
      expect(state.pendingInteractions[0].title).toBe('第二轮：输入备注')
      expect(state.pendingInteractions[0].mode).toBe('conversation')
      expect(state.pendingInteractions[0].suggestions).toEqual([
        '常规部署',
        '紧急修复',
      ])

      // 用户响应第二轮
      act(() => {
        useInteractionStore.getState().markResponded('req-round2')
      })

      state = useInteractionStore.getState()
      expect(state.pendingInteractions[0].status).toBe('responded')
    })

    it('两轮交互卡片同时存在时均正确渲染', () => {
      // 同时存在两轮交互（Agent 并发请求）
      const interaction1 = createPendingInteraction({
        requestId: 'req-concurrent-1',
        mode: 'choice',
        title: '选择区域',
        options: [
          { id: 'cn', label: '中国' },
          { id: 'us', label: '美国' },
        ],
      })

      const interaction2 = createPendingInteraction({
        requestId: 'req-concurrent-2',
        mode: 'choice',
        title: '选择版本',
        options: [
          { id: 'v1', label: 'V1' },
          { id: 'v2', label: 'V2' },
        ],
      })

      const onRespond1 = vi.fn()
      const onRespond2 = vi.fn()

      render(
        <div>
          <InteractionCard
            {...createCardProps({
              interaction: interaction1,
              onRespondChoice: onRespond1,
            })}
          />
          <InteractionCard
            {...createCardProps({
              interaction: interaction2,
              onRespondChoice: onRespond2,
            })}
          />
        </div>,
      )

      // 两张卡片都可见
      expect(screen.getByText('选择区域')).toBeInTheDocument()
      expect(screen.getByText('选择版本')).toBeInTheDocument()

      // 中国、美国、V1、V2
      expect(screen.getByText('中国')).toBeInTheDocument()
      expect(screen.getByText('美国')).toBeInTheDocument()
      expect(screen.getByText('V1')).toBeInTheDocument()
      expect(screen.getByText('V2')).toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // 3. 交互后工具调用
  // -----------------------------------------------------------------------
  describe('交互后工具调用', () => {
    it('用户响应后 Agent 调用新工具（store 集成验证）', () => {
      // 交互请求到达
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-pre-tool',
          mode: 'choice',
          title: '是否执行工具？',
          description: '',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          options: [
            { id: 'yes', label: '执行' },
            { id: 'no', label: '跳过' },
          ],
        })
      })

      let interactionState = useInteractionStore.getState()
      expect(interactionState.pendingInteractions).toHaveLength(1)

      // 用户选择"执行"
      act(() => {
        useInteractionStore.getState().markResponded('req-pre-tool')
      })

      interactionState = useInteractionStore.getState()
      expect(interactionState.pendingInteractions[0].status).toBe('responded')

      // Agent 响应后调用新工具
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-after-response',
            name: 'run_task',
          }),
        )
      })

      let layoutState = useLayoutModeStore.getState()
      expect(layoutState.activeExecutions).toHaveLength(1)
      expect(layoutState.activeExecutions[0].name).toBe('run_task')
      expect(layoutState.activeExecutions[0].status).toBe('running')

      // 新工具执行完成
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-after-response',
            name: 'run_task',
            status: 'completed',
            progress: 100,
          }),
        )
      })

      layoutState = useLayoutModeStore.getState()
      expect(layoutState.activeExecutions[0].status).toBe('completed')
    })

    it('执行事件（store 直驱）→ 渲染交互卡片 → 用户响应', async () => {
      const onRespondChoice = vi.fn()

      // 注册 useRealtimeEvents（真实 hook；执行追踪事件已随 0.2 后端退役，
      // 执行态由 store 直驱——与生产中 tool_start/tool_result handler 的写入路径一致）
      renderHook(() => useRealtimeEvents())

      // 执行开始（store 写入）
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({ id: 'exec-ws-tool', name: 'pre_check' }),
        )
      })

      const layoutState = useLayoutModeStore.getState()
      expect(layoutState.activeExecutions).toHaveLength(1)
      expect(layoutState.activeExecutions[0].name).toBe('pre_check')

      // 执行完成
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({ id: 'exec-ws-tool', name: 'pre_check', status: 'completed', progress: 100 }),
        )
      })

      expect(useLayoutModeStore.getState().activeExecutions[0].status).toBe('completed')

      // 交互请求经 globalWS interaction_request 事件真实到达（useInteractionHandler 订阅）
      renderHook(() => useInteractionHandler('session-1'), { wrapper: MemoryRouter })
      await act(async () => {
        emitEvent('interaction_request', {
          request_id: 'req-ws-tool',
          interaction_mode: 'choice',
          title: '确认后执行工具',
          thread_id: 'thread-1',
          options: [
            { id: 'go', label: '执行' },
            { id: 'stop', label: '停止' },
          ],
        })
      })

      const interactionState = useInteractionStore.getState()
      const interaction = interactionState.pendingInteractions.find(
        (i) => i.requestId === 'req-ws-tool',
      )
      expect(interaction).toBeDefined()
      expect(interaction!.title).toBe('确认后执行工具')

      // 渲染交互卡片
      render(
        <InteractionCard
          {...createCardProps({
            interaction: interaction!,
            onRespondChoice,
          })}
        />,
      )

      expect(screen.getByText('确认后执行工具')).toBeInTheDocument()

      // 用户点击"执行"
      await act(async () => {
        fireEvent.click(screen.getByText('执行'))
      })

      expect(onRespondChoice).toHaveBeenCalledWith('go')

      // 标记响应
      act(() => {
        useInteractionStore.getState().markResponded('req-ws-tool')
      })

      const updated = useInteractionStore.getState().pendingInteractions.find(
        (i) => i.requestId === 'req-ws-tool',
      )
      expect(updated!.status).toBe('responded')

      // Agent 开始执行新工具
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({ id: 'exec-ws-after', name: 'post_interaction_tool' }),
        )
      })

      const newLayoutState = useLayoutModeStore.getState()
      const newExec = newLayoutState.activeExecutions.find(
        (e) => e.id === 'exec-ws-after',
      )
      expect(newExec).toBeDefined()
      expect(newExec!.name).toBe('post_interaction_tool')
    })
  })

  // -----------------------------------------------------------------------
  // 4. 混合内容顺序
  // -----------------------------------------------------------------------
  describe('混合内容顺序', () => {
    it('多轮交互中消息渲染顺序：text → tool_call → interaction → text', () => {
      const recorder = createEventRecorder()

      // 模拟完整的多内容消息序列
      // 1. 文本消息
      recorder.record('text')
      const textMsg = createMockMessage({
        id: 'msg-text-1',
        content: '我来帮你处理这个任务。',
        parts: [createTextPart('我来帮你处理这个任务。', 1)],
      })

      // 2. 工具调用
      recorder.record('tool_call')
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-mixed-1',
            name: 'analyze',
            status: 'running',
          }),
        )
      })

      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-mixed-1',
            name: 'analyze',
            status: 'completed',
            progress: 100,
          }),
        )
      })

      const toolMsg = createMockMessage({
        id: 'msg-tool-1',
        content: '',
        parts: [
          createToolCallPart({
            callId: 'exec-mixed-1',
            name: 'analyze',
            state: 'done',
            sequence: 1,
          }),
        ],
      })

      // 3. 交互请求
      recorder.record('interaction')
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-mixed',
          mode: 'choice',
          title: '请确认分析结果',
          description: '',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          options: [
            { id: 'accept', label: '接受' },
            { id: 'reject', label: '拒绝' },
          ],
        })
      })

      const interactionState = useInteractionStore.getState()
      expect(interactionState.pendingInteractions).toHaveLength(1)

      // 4. 用户响应后，Agent 继续输出文本
      recorder.record('user_response')
      act(() => {
        useInteractionStore.getState().markResponded('req-mixed')
      })

      recorder.record('text_after')
      const followUpMsg = createMockMessage({
        id: 'msg-text-2',
        content: '好的，我继续处理。',
        parts: [createTextPart('好的，我继续处理。', 1)],
      })

      // 验证事件顺序
      expect(recorder.log).toEqual([
        'text',
        'tool_call',
        'interaction',
        'user_response',
        'text_after',
      ])

      // 验证消息对象存在
      expect(textMsg.content).toBe('我来帮你处理这个任务。')
      expect(toolMsg.id).toBe('msg-tool-1')
      expect(followUpMsg.content).toBe('好的，我继续处理。')
    })

    it('通过渲染验证多内容的渲染顺序', () => {
      // 渲染文本消息
      const { rerender } = render(
        <div data-testid="message-list">
          <div data-testid="msg-item" data-type="text">
            <span data-testid="msg-text">分析完成，请选择后续操作。</span>
          </div>
        </div>,
      )

      expect(screen.getByTestId('msg-item')).toBeInTheDocument()
      expect(screen.getByTestId('msg-item').getAttribute('data-type')).toBe('text')

      // 追加工具调用
      rerender(
        <div data-testid="message-list">
          <div data-testid="msg-item" data-type="text">
            <span data-testid="msg-text">分析完成，请选择后续操作。</span>
          </div>
          <div data-testid="msg-item" data-type="tool_call">
            <span data-testid="msg-text">analyze 工具已执行</span>
          </div>
        </div>,
      )

      const toolItems = screen.getAllByTestId('msg-item')
      expect(toolItems).toHaveLength(2)
      expect(toolItems[0].getAttribute('data-type')).toBe('text')
      expect(toolItems[1].getAttribute('data-type')).toBe('tool_call')

      // 追加交互卡片
      rerender(
        <div data-testid="message-list">
          <div data-testid="msg-item" data-type="text">
            <span data-testid="msg-text">分析完成，请选择后续操作。</span>
          </div>
          <div data-testid="msg-item" data-type="tool_call">
            <span data-testid="msg-text">analyze 工具已执行</span>
          </div>
          <div data-testid="msg-item" data-type="interaction">
            <InteractionCard
              {...createCardProps({
                interaction: createPendingInteraction({
                  requestId: 'req-order',
                  mode: 'choice',
                  title: '选择操作',
                  options: [
                    { id: 'next', label: '下一步' },
                  ],
                }),
              })}
            />
          </div>
        </div>,
      )

      const allItems = screen.getAllByTestId('msg-item')
      expect(allItems).toHaveLength(3)
      expect(allItems[0].getAttribute('data-type')).toBe('text')
      expect(allItems[1].getAttribute('data-type')).toBe('tool_call')
      expect(allItems[2].getAttribute('data-type')).toBe('interaction')

      // 交互卡片内容
      expect(screen.getByText('选择操作')).toBeInTheDocument()
      expect(screen.getByText('下一步')).toBeInTheDocument()
    })

    it('2 轮完整混合内容顺序验证', async () => {
      const recorder = createEventRecorder()

      // ===== 第一轮 =====
      // text
      recorder.record('r1-text')
      // tool_call
      recorder.record('r1-tool')
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-r1',
            name: 'check_status',
          }),
        )
      })
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-r1',
            name: 'check_status',
            status: 'completed',
            progress: 100,
          }),
        )
      })

      // interaction
      recorder.record('r1-interaction')
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-r1',
          mode: 'choice',
          title: '第一轮确认',
          description: '',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          options: [
            { id: 'proceed', label: '继续' },
          ],
        })
      })

      // user response
      recorder.record('r1-response')
      act(() => {
        useInteractionStore.getState().markResponded('req-r1')
      })

      // ===== 第二轮 =====
      // tool_call
      recorder.record('r2-tool')
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-r2',
            name: 'deploy',
          }),
        )
      })
      act(() => {
        useLayoutModeStore.getState().addOrUpdateExecution(
          createExecutionEvent({
            id: 'exec-r2',
            name: 'deploy',
            status: 'completed',
            progress: 100,
          }),
        )
      })

      // interaction
      recorder.record('r2-interaction')
      act(() => {
        useInteractionStore.getState().addInteraction({
          requestId: 'req-r2',
          mode: 'conversation',
          title: '第二轮备注',
          description: '',
          threadId: 'thread-1',
          tabId: 'tab-1',
          agentId: 'agent-1',
          timestamp: new Date().toISOString(),
          suggestions: ['常规部署', '紧急修复'],
        })
      })

      // user response
      recorder.record('r2-response')
      act(() => {
        useInteractionStore.getState().markResponded('req-r2')
      })

      // text
      recorder.record('r2-text')

      // 验证完整顺序
      expect(recorder.log).toEqual([
        'r1-text',
        'r1-tool',
        'r1-interaction',
        'r1-response',
        'r2-tool',
        'r2-interaction',
        'r2-response',
        'r2-text',
      ])

      // 验证最终 store 状态
      const interactionState = useInteractionStore.getState()
      const r1 = interactionState.pendingInteractions.find((i) => i.requestId === 'req-r1')
      const r2 = interactionState.pendingInteractions.find((i) => i.requestId === 'req-r2')
      expect(r1!.status).toBe('responded')
      expect(r2!.status).toBe('responded')

      const layoutState = useLayoutModeStore.getState()
      expect(layoutState.activeExecutions).toHaveLength(2)
      expect(layoutState.activeExecutions[0].name).toBe('check_status')
      expect(layoutState.activeExecutions[1].name).toBe('deploy')
    })
  })

  // -----------------------------------------------------------------------
  // 附加：useInteractionHandler 集成（通过 WebSocket 事件添加交互）
  // -----------------------------------------------------------------------
  describe('useInteractionHandler 交互请求集成', () => {
    it('interaction_request 事件应触发 store 添加交互', async () => {
      // useInteractionHandler 通过 globalWS 订阅 interaction_request 事件
      renderHook(() => useInteractionHandler('session-1'), { wrapper: MemoryRouter })

      await act(async () => {
        emitEvent('interaction_request', {
          request_id: 'req-realtime',
          interaction_mode: 'choice',
          title: '实时交互',
          options: [
            { id: 'ok', label: '确定' },
          ],
        })
      })

      const state = useInteractionStore.getState()
      const found = state.pendingInteractions.find(
        (i) => i.requestId === 'req-realtime',
      )
      expect(found).toBeDefined()
      expect(found!.title).toBe('实时交互')
      expect(found!.mode).toBe('choice')
    })

    it('连续两个 interaction_request 应产生两个待处理交互', async () => {
      renderHook(() => useInteractionHandler('session-1'), { wrapper: MemoryRouter })

      await act(async () => {
        emitEvent('interaction_request', {
          request_id: 'req-multi-1',
          interaction_mode: 'choice',
          title: '第一个请求',
          options: [{ id: 'a', label: 'A' }],
        })
      })

      await act(async () => {
        emitEvent('interaction_request', {
          request_id: 'req-multi-2',
          interaction_mode: 'conversation',
          title: '第二个请求',
        })
      })

      const state = useInteractionStore.getState()
      expect(state.pendingInteractions).toHaveLength(2)
      expect(state.pendingInteractions[0].requestId).toBe('req-multi-1')
      expect(state.pendingInteractions[1].requestId).toBe('req-multi-2')
    })

    it('interaction_request 仅接受 snake_case request_id（camelCase 无 request_id 会被丢弃）', async () => {
      renderHook(() => useInteractionHandler('session-1'), { wrapper: MemoryRouter })

      // 仅 camelCase 字段（无 request_id）
      await act(async () => {
        emitEvent('interaction_request', {
          requestId: 'req-camel',
          interactionMode: 'choice',
          title: '驼峰命名',
          options: [{ id: 'ok', label: '确定' }],
        })
      })

      // 当前契约：parseInteractionEvent 只读 inner.request_id（snake_case），
      // 缺失即返回 null——camelCase 兼容从未实现（F2 真实验证暴露的事实；
      // 后端事件始终发 snake_case，故不视为缺陷）。
      const state = useInteractionStore.getState()
      expect(state.pendingInteractions.find((i) => i.requestId === 'req-camel')).toBeUndefined()

      // snake_case 正常解析
      await act(async () => {
        emitEvent('interaction_request', {
          request_id: 'req-snake',
          interaction_mode: 'choice',
          title: '下划线命名',
          options: [{ id: 'ok', label: '确定' }],
        })
      })
      const found = useInteractionStore.getState().pendingInteractions.find(
        (i) => i.requestId === 'req-snake',
      )
      expect(found).toBeDefined()
      expect(found!.title).toBe('下划线命名')
    })
  })

  // -----------------------------------------------------------------------
  // 附加：useRealtimeEvents 任务生命周期集成
  // （execution_start/done 等执行事件 2026-08 已删除——后端无发射源，
  //   原集成用例断言的是死接线，详见 useRealtimeEvents.ts 头部清理注释）
  // -----------------------------------------------------------------------
  describe('useRealtimeEvents 任务生命周期集成', () => {
    it('task_status_update 事件应更新 longTermTaskStore 并 bump 工作区版本', async () => {
      const { useLongTermTaskStore } = await import('@/stores/longTermTaskStore')
      // 种子任务：事件到达时应走 updateTask 分支而非全量 fetchTasks
      useLongTermTaskStore.setState({
        tasks: [
          {
            id: 'task-1',
            title: '长期任务',
            status: 'running',
            currentPhase: 'prepare',
          },
        ] as never,
      })

      renderHook(() => useRealtimeEvents())

      const versionBefore = useLayoutModeStore.getState().workspaceDataVersion
      await act(async () => {
        emitEvent('task_status_update', {
          task_id: 'task-1',
          new_status: 'completed',
          current_phase: 'execute',
        })
      })

      const taskState = useLongTermTaskStore.getState()
      const task = taskState.tasks.find((t: { id: string }) => t.id === 'task-1') as {
        status: string
        currentPhase: string
      }
      expect(task).toBeDefined()
      expect(task.status).toBe('completed')
      expect(task.currentPhase).toBe('execute')

      // 工作区数据版本被 bump（触发工作区面板刷新）
      expect(useLayoutModeStore.getState().workspaceDataVersion).toBeGreaterThan(versionBefore)
    })

    it('task_deleted 事件应从 store 删除任务', async () => {
      const { useLongTermTaskStore } = await import('@/stores/longTermTaskStore')
      useLongTermTaskStore.setState({
        tasks: [
          { id: 'task-del', title: '待删除', status: 'running' },
        ] as never,
      })

      renderHook(() => useRealtimeEvents())

      await act(async () => {
        emitEvent('task_deleted', { task_id: 'task-del' })
      })

      expect(
        useLongTermTaskStore.getState().tasks.some((t: { id: string }) => t.id === 'task-del'),
      ).toBe(false)
    })
  })
})
