/**
 * 端到端测试共享工具
 *
 * 提供 MockWebSocketService、事件数据工厂、消息工厂、渲染辅助函数等。
 * 所有聊天组件的端到端测试共享此文件。
 *
 * @module testUtils
 */

import { render, renderHook } from '@testing-library/react'
import React from 'react'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import type { InteractionCardProps } from '@/components/chat/InteractionCard'
import type { SubAgentCardProps } from '@/components/chat/SubAgentCard'
import type { ActivityCardProps, ActivityData  } from '@/types/activity'
import type { Message, MessageToolCall, ThinkingContent } from '@/types/models'
import type { MessagePart, PartState, ToolCallPartState } from '@/types/messageParts'
import type { RenderOptions } from '@testing-library/react'

// ============================================================
// MockWebSocketService
// ============================================================

/**
 * 模拟 WebSocketService，提供事件注册与手动触发能力。
 *
 * 内部维护 eventHandlers Map<string, Set<Function>>，
 * 与真实 WebSocketService 的 subscribe/unsubscribe 模式对齐。
 */
export class MockWebSocketService {
  /** 事件处理器映射：eventType -> Set<handler> */
  private eventHandlers: Map<string, Set<(...args: unknown[]) => void>> = new Map()

  /**
   * 订阅事件
   *
   * @param event - 事件类型
   * @param handler - 事件处理函数
   */
  subscribe(event: string, handler: (...args: unknown[]) => void): void {
    if (!this.eventHandlers.has(event)) {
      this.eventHandlers.set(event, new Set())
    }
    this.eventHandlers.get(event)!.add(handler)
  }

  /**
   * 取消订阅事件
   *
   * @param event - 事件类型
   * @param handler - 事件处理函数
   */
  unsubscribe(event: string, handler: (...args: unknown[]) => void): void {
    const handlers = this.eventHandlers.get(event)
    if (handlers) {
      handlers.delete(handler)
    }
  }

  /**
   * 发送消息（空实现，仅记录调用）
   *
   * @param _message - 要发送的消息
   */
  send(_message: unknown): void {
    // Mock: no-op
  }

  /**
   * 手动触发一个事件，通知所有注册的处理器
   *
   * @param eventType - 事件类型
   * @param data - 事件数据
   */
  trigger(eventType: string, data: unknown): void {
    const handlers = this.eventHandlers.get(eventType)
    if (handlers) {
      for (const handler of handlers) {
        handler(data)
      }
    }
  }

  /**
   * 按序触发多个事件
   *
   * @param events - 事件序列，每项包含 type 和 data
   */
  triggerSequence(events: Array<{ type: string; data: unknown }>): void {
    for (const event of events) {
      this.trigger(event.type, event.data)
    }
  }

  /**
   * 清除所有已注册的事件处理器
   */
  clearAll(): void {
    this.eventHandlers.clear()
  }

  /**
   * 获取指定事件的处理器数量（用于断言）
   */
  getHandlerCount(event: string): number {
    return this.eventHandlers.get(event)?.size ?? 0
  }
}

/** 全局 MockWebSocketService 实例 */
export const mockWsService = new MockWebSocketService()

// ============================================================
// 事件数据工厂函数
// ============================================================

/**
 * 创建流式输出开始事件
 */
export function createStreamStartEvent(messageId: string, threadId: string) {
  return {
    type: 'stream_start' as const,
    message_id: messageId,
    thread_id: threadId,
  }
}

/**
 * 创建流式输出片段事件
 */
export function createStreamChunkEvent(messageId: string, threadId: string, chunk: string) {
  return {
    type: 'stream_chunk' as const,
    message_id: messageId,
    thread_id: threadId,
    content: chunk,
  }
}

/**
 * 创建流式输出结束事件
 */
export function createStreamEndEvent(messageId: string, threadId: string) {
  return {
    type: 'stream_end' as const,
    message_id: messageId,
    thread_id: threadId,
  }
}

/**
 * 创建流式输出错误事件
 */
export function createStreamErrorEvent(messageId: string, threadId: string, error: string) {
  return {
    type: 'stream_error' as const,
    message_id: messageId,
    thread_id: threadId,
    error,
  }
}

/**
 * 创建思考开始事件
 */
export function createThinkingStartEvent(messageId: string, threadId: string) {
  return {
    type: 'thinking_start' as const,
    message_id: messageId,
    thread_id: threadId,
  }
}

/**
 * 创建思考内容片段事件
 */
export function createThinkingChunkEvent(messageId: string, threadId: string, chunk: string) {
  return {
    type: 'thinking_chunk' as const,
    message_id: messageId,
    thread_id: threadId,
    content: chunk,
  }
}

/**
 * 创建思考结束事件
 */
export function createThinkingEndEvent(messageId: string, threadId: string) {
  return {
    type: 'thinking_end' as const,
    message_id: messageId,
    thread_id: threadId,
  }
}

/**
 * 创建执行开始事件
 */
export function createExecutionStartEvent(
  executionId: string,
  name: string,
  executionType: 'tool' | 'agent' | 'workflow' = 'tool',
) {
  return {
    type: 'execution_start' as const,
    execution_id: executionId,
    name,
    execution_type: executionType,
  }
}

/**
 * 创建执行进度事件
 */
export function createExecutionProgressEvent(
  executionId: string,
  progress: number,
  currentStep?: string,
) {
  return {
    type: 'execution_progress' as const,
    execution_id: executionId,
    progress,
    current_step: currentStep,
  }
}

/**
 * 创建执行完成事件
 */
export function createExecutionDoneEvent(
  executionId: string,
  success: boolean,
  output?: Record<string, unknown>,
  error?: string,
) {
  return {
    type: 'execution_done' as const,
    execution_id: executionId,
    success,
    output,
    error,
  }
}

/**
 * 创建执行取消事件
 */
export function createExecutionCancelledEvent(executionId: string, reason: string) {
  return {
    type: 'execution_cancelled' as const,
    execution_id: executionId,
    reason,
  }
}

/**
 * 创建交互请求事件
 */
export function createInteractionRequestEvent(
  requestId: string,
  mode: 'choice' | 'conversation',
  title: string,
  options?: Array<{ id: string; label: string }>,
  suggestions?: string[],
) {
  return {
    type: 'interaction_request' as const,
    request_id: requestId,
    mode,
    title,
    options,
    suggestions,
  }
}

/**
 * 创建子 Agent 创建事件
 */
export function createSubAgentCreatedEvent(
  agentId: string,
  agentName: string,
  level: 1 | 2 | 3,
  parentAgentId: string,
) {
  return {
    type: 'sub_agent_created' as const,
    agentId,
    agentName,
    agentLevel: level,
    parentAgentId,
  }
}

/**
 * 创建子 Agent 等待输入事件
 */
export function createSubAgentWaitingInputEvent(
  agentId: string,
  agentName: string,
  prompt: string,
) {
  return {
    type: 'sub_agent_waiting_input' as const,
    agentId,
    agentName,
    prompt,
  }
}

/**
 * 创建子 Agent 完成事件
 */
export function createSubAgentCompletedEvent(
  agentId: string,
  agentName: string,
  success: boolean,
  summary?: string,
) {
  return {
    type: 'sub_agent_completed' as const,
    agentId,
    agentName,
    success,
    summary,
  }
}

/**
 * 创建 Agent 层级变更事件
 */
export function createAgentLevelChangedEvent(
  agentId: string,
  oldLevel: 1 | 2 | 3,
  newLevel: 1 | 2 | 3,
) {
  return {
    type: 'agent_level_changed' as const,
    agentId,
    oldLevel,
    newLevel,
  }
}

// ============================================================
// 消息工厂函数
// ============================================================

/**
 * 创建模拟消息对象
 *
 * @param overrides - 部分覆盖默认消息属性
 * @returns 完整的 Message 对象
 */
export function createMockMessage(overrides?: Partial<Message>): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...overrides,
  }
}

/**
 * 创建模拟工具调用对象
 *
 * @param overrides - 部分覆盖默认工具调用属性
 * @returns 完整的 MessageToolCall 对象
 */
export function createMockToolCall(overrides?: Partial<MessageToolCall>): MessageToolCall {
  return {
    call_id: 'call-1',
    tool_name: 'search',
    tool_args: {},
    status: 'completed',
    ...overrides,
  }
}

// ============================================================
// MessagePart 工厂函数（统一渲染模型）
// ============================================================

/**
 * 创建文本 Part
 *
 * @param content - 文本内容
 * @param sequence - 序号（默认 1）
 * @param state - Part 状态（默认 'done'）
 * @returns TextPart 对象
 */
export function createTextPart(
  content: string,
  sequence: number = 1,
  state: PartState = 'done',
): MessagePart {
  return { type: 'text', content, state, sequence }
}

/**
 * 创建思考 Part
 *
 * @param content - 思考内容
 * @param sequence - 序号（默认 1）
 * @param state - Part 状态（默认 'done'）
 * @returns ThinkingPart 对象
 */
export function createThinkingPart(
  content: string,
  sequence: number = 1,
  state: PartState = 'done',
): MessagePart {
  return { type: 'thinking', content, state, sequence }
}

/**
 * 创建工具调用 Part
 *
 * @param overrides - 部分覆盖工具调用 Part 属性
 * @returns ToolCallPart 对象
 */
export function createToolCallPart(overrides: {
  callId: string
  name: string
  args?: Record<string, unknown>
  state?: ToolCallPartState
  result?: unknown
  error?: string
  durationMs?: number
  sequence?: number
  progress?: number
  currentStep?: string
}): MessagePart {
  return {
    type: 'tool_call',
    callId: overrides.callId,
    name: overrides.name,
    args: overrides.args ?? {},
    state: overrides.state ?? 'done',
    result: overrides.result,
    error: overrides.error,
    durationMs: overrides.durationMs,
    sequence: overrides.sequence ?? 1,
    progress: overrides.progress,
    currentStep: overrides.currentStep,
  }
}

// ============================================================
// 渲染辅助函数
// ============================================================

/**
 * 渲染 MessageContentRenderer 的辅助函数
 *
 * 直接测试 useMessageRender hook 的输出 fragments，
 * 并通过 MessageContentRenderer 渲染到 DOM。
 *
 * @param fragments - 渲染片段数组
 * @param options - render 选项及 MessageContentRenderer props
 * @returns render 结果
 */
export async function renderMessageContent(
  fragments: RenderFragment[],
  options?: {
    isStreaming?: boolean
    renderOptions?: RenderOptions
  },
) {
  // 动态导入以利用模块 mock
  const { MessageContentRenderer } = await import(
    '@/components/chat/MessageContentRenderer'
  )

  return render(
    React.createElement(MessageContentRenderer, {
      fragments,
      isStreaming: options?.isStreaming ?? false,
    }),
    options?.renderOptions,
  )
}

/**
 * 渲染 InteractionCard 的辅助函数
 *
 * @param props - InteractionCardProps 的部分属性
 * @returns render 结果
 */
export async function renderInteractionCard(props: Partial<InteractionCardProps>) {
  const { InteractionCard } = await import('@/components/chat/InteractionCard')

  const fullProps: InteractionCardProps = {
    interaction: {
      requestId: 'req-1',
      mode: 'choice',
      title: '交互请求',
      description: '',
      threadId: 'thread-1',
      tabId: 'tab-1',
      agentId: 'agent-1',
      timestamp: new Date().toISOString(),
      status: 'pending',
      ...props.interaction,
    },
    onRespondChoice: props.onRespondChoice ?? (() => {}),
    onRespondText: props.onRespondText ?? (() => {}),
    onNavigateToTab: props.onNavigateToTab ?? (() => {}),
    isSubmitting: props.isSubmitting ?? false,
  }

  return render(React.createElement(InteractionCard, fullProps))
}

/**
 * 渲染 SubAgentCard 的辅助函数
 *
 * @param props - SubAgentCardProps 的部分属性
 * @returns render 结果
 */
export async function renderSubAgentCard(props: Partial<SubAgentCardProps>) {
  const { SubAgentCard } = await import('@/components/chat/SubAgentCard')

  const fullProps: SubAgentCardProps = {
    data: {
      id: 'agent-1',
      name: '子 Agent',
      agentLevel: 2,
      status: 'running',
      ...props.data,
    },
    mode: props.mode ?? 'summary',
    expandable: props.expandable ?? true,
    onExpand: props.onExpand,
    onOpenDetail: props.onOpenDetail,
    className: props.className,
  }

  return render(React.createElement(SubAgentCard, fullProps))
}

/**
 * 渲染 ActivityCard 的辅助函数
 *
 * @param props - ActivityCardProps 的部分属性
 * @returns render 结果
 */
export async function renderActivityCard(props: Partial<ActivityCardProps>) {
  // ActivityCard 是 default export
  const ActivityCardModule = await import('@/components/chat/ActivityCard')
  const ActivityCard = ActivityCardModule.default

  const defaultActivity: ActivityData = {
    type: 'tool_call',
    id: 'activity-1',
    title: '工具调用',
    status: 'running',
    ...props.activity,
  }

  return render(
    React.createElement(ActivityCard, {
      activity: defaultActivity,
      defaultExpanded: props.defaultExpanded ?? false,
      onHeaderClick: props.onHeaderClick,
      className: props.className,
      style: props.style,
    }),
  )
}

// ============================================================
// WebSocket mock 注入
// ============================================================

/**
 * 注入 mock WebSocketService 到模块系统
 *
 * 在测试 beforeEach 中调用，使所有 import WebSocketService 的模块
 * 获得模拟版本。
 */
export function setupWebSocketMock(): void {
  vi.mock('@/services/websocket/WebSocketService', () => ({
    webSocketService: mockWsService,
    WebSocketService: MockWebSocketService,
    default: mockWsService,
  }))
}

/**
 * 清理 mock WebSocketService
 *
 * 在测试 afterEach 中调用，清除所有注册的事件处理器。
 */
export function cleanupWebSocketMock(): void {
  mockWsService.clearAll()
}

/**
 * useMessageRender hook 的参数类型
 */
interface UseMessageRenderProps {
  message: Message
  isLast?: boolean
  isGenerating?: boolean
  versionContent?: string | null
}

/**
 * 使用 renderHook 测试 useMessageRender hook 的辅助函数
 *
 * 使用 initialProps 模式，确保 rerender 时 hook 能正确接收新参数。
 *
 * @param message - 消息对象
 * @param options - hook 选项
 * @returns renderHook 结果（含 rerender 方法）
 */
export async function renderUseMessageRender(
  message: Message,
  options?: {
    isLast?: boolean
    isGenerating?: boolean
    versionContent?: string | null
  },
) {
  const { useMessageRender } = await import(
    '@/components/chat/hooks/useMessageRender'
  )

  const initialProps: UseMessageRenderProps = {
    message,
    isLast: options?.isLast ?? false,
    isGenerating: options?.isGenerating ?? false,
    versionContent: options?.versionContent,
  }

  return renderHook(
    (props: UseMessageRenderProps) =>
      useMessageRender({
        message: props.message,
        isLast: props.isLast ?? false,
        isGenerating: props.isGenerating ?? false,
        versionContent: props.versionContent,
      }),
    { initialProps },
  )
}
