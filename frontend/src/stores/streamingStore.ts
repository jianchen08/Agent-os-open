import { create } from 'zustand'
import { loggers } from '@/utils/logger'
import { usePipelineMessageStore } from './pipelineMessageStore'
import type { Message, MessageToolCall, ThinkingStep, ToolCallStatus } from '@/types/models'

const logger = loggers.sessionStore

interface StreamingState {
  /** @deprecated 使用 isTabStreaming 替代，保留用于向后兼容 */
  isStreaming: boolean
  refreshingMessageId: string | null
  /** 每个标签页的 streaming 状态 (tabId -> isStreaming) */
  streamingTabs: Record<string, boolean>

  setRefreshingMessageId: (messageId: string | null) => void
  /** 为指定标签页设置 streaming 状态 */
  setStreamingForTab: (tabId: string, isStreaming: boolean) => void
  /** 查询指定标签页是否正在 streaming */
  isTabStreaming: (tabId: string) => boolean
  /** 结束指定标签页的 streaming 状态 */
  stopStreamingForTab: (tabId: string) => void
  stopStreaming: () => void
  endThinking: (pipelineId: string, messageId: string, durationMs?: number) => void
  updateThinkingStep: (pipelineId: string, messageId: string, step: ThinkingStep) => void
  addToolCallToMessage: (
    pipelineId: string,
    messageId: string,
    toolCall: MessageToolCall,
    parentId?: string,
  ) => void
  updateToolCallInMessage: (
    pipelineId: string,
    messageId: string,
    callId: string,
    updates: Partial<MessageToolCall>,
  ) => void
  updateToolCallProgress: (
    pipelineId: string,
    messageId: string,
    toolCallId: string,
    progress: number,
    currentStep?: string,
    estimatedRemainingMs?: number,
  ) => void
  appendToolCallOutput: (
    pipelineId: string,
    messageId: string,
    toolCallId: string,
    output: string,
  ) => void
}

export const useStreamingStore = create<StreamingState>()((set, get) => ({
  isStreaming: false,
  refreshingMessageId: null,
  streamingTabs: {},

  /** BUG-FIX-fix_20260506_per_tab_streaming: 为指定标签页设置 streaming 状态 */
  setStreamingForTab: (tabId: string, isStreaming: boolean) => {
    const current = get().streamingTabs[tabId]
    if (current === isStreaming) return

    const newStreamingTabs = { ...get().streamingTabs, [tabId]: isStreaming }
    if (!isStreaming) {
      delete newStreamingTabs[tabId]
    }

    const anyStreaming = Object.values(newStreamingTabs).some(Boolean)
    set({
      streamingTabs: newStreamingTabs,
      isStreaming: anyStreaming,
    })
  },

  /** BUG-FIX-fix_20260506_per_tab_streaming: 查询指定标签页是否正在 streaming */
  isTabStreaming: (tabId: string) => {
    return get().streamingTabs[tabId] ?? false
  },

  /** BUG-FIX-fix_20260506_per_tab_streaming: 结束指定标签页的 streaming 状态 */
  stopStreamingForTab: (tabId: string) => {
    get().setStreamingForTab(tabId, false)
  },

  setRefreshingMessageId: (messageId: string | null) => {
    set({ refreshingMessageId: messageId })
  },

  /**
   * 停止所有 streaming 状态，清理 pipelineMessageStore 中残留的 streaming/thinking 消息
   */
  stopStreaming: () => {
    const pipelineStore = usePipelineMessageStore.getState()

    // 遍历所有管道，清理 streaming 和 thinking 状态
    const updatedMessagesByPipeline = { ...pipelineStore.messagesByPipeline }
    for (const pipelineId of Object.keys(updatedMessagesByPipeline)) {
      const pipelineMessages = updatedMessagesByPipeline[pipelineId]
      const needsUpdate = pipelineMessages.some(
        (m) => m.status === 'streaming' || m.thinking?.isThinking,
      )

      if (needsUpdate) {
        updatedMessagesByPipeline[pipelineId] = pipelineMessages.map((message) => {
          const updates: Partial<Message> = {}

          if (message.status === 'streaming') {
            updates.status = 'completed'
          }

          if (message.thinking?.isThinking) {
            updates.thinking = {
              ...message.thinking,
              isThinking: false,
            }
          }

          // 清理 contentBlocks 中残留的 thinking block
          if ((message.contentBlocks || []).some((b: any) => b.type === 'thinking' && b.thinking?.isThinking)) {
            updates.contentBlocks = (message.contentBlocks || []).map((b: any) => {
              if (b.type === 'thinking' && b.thinking?.isThinking) {
                return { ...b, thinking: { ...b.thinking, isThinking: false } }
              }
              return b
            })
          }

          if (Object.keys(updates).length > 0) {
            return { ...message, ...updates }
          }
          return message
        })
      }
    }

    usePipelineMessageStore.setState({ messagesByPipeline: updatedMessagesByPipeline })
    set({ isStreaming: false, refreshingMessageId: null, streamingTabs: {} })
  },

  /**
   * 结束指定管道中消息的思考状态
   */
  endThinking: (pipelineId: string, messageId: string, durationMs?: number) => {
    usePipelineMessageStore.setState((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = pipelineMessages[messageIndex]
      if (!message.thinking) {
        return state
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...message,
        thinking: {
          ...message.thinking,
          isThinking: false,
          durationMs,
        },
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /**
   * 更新指定管道中消息的思考步骤
   */
  updateThinkingStep: (pipelineId: string, messageId: string, step: ThinkingStep) => {
    usePipelineMessageStore.setState((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = pipelineMessages[messageIndex]
      const thinking = message.thinking || {
        content: '',
        isThinking: false,
      }
      const steps = thinking.steps || []

      const stepIndex = steps.findIndex((s) => s.id === step.id)
      let updatedSteps: ThinkingStep[]

      if (stepIndex >= 0) {
        updatedSteps = [...steps]
        updatedSteps[stepIndex] = step
      } else {
        updatedSteps = [...steps, step]
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...message,
        thinking: {
          ...thinking,
          steps: updatedSteps,
          currentStepIndex: updatedSteps.length - 1,
        },
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /**
   * 向指定管道的消息添加工具调用
   */
  addToolCallToMessage: (
    pipelineId: string,
    messageId: string,
    toolCall: MessageToolCall,
    parentId?: string,
  ) => {
    usePipelineMessageStore.setState((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        // BUG-FIX-fix_20260513_ai_msg_duplicate:
        // 问题根因: 直接用 setState 创建消息，绕过了 addMessage 的 ID 去重逻辑，
        //          如果 handleStreamStart 已创建同 ID 消息，会导致重复。
        // 修复方案: 不在此处创建消息占位，直接返回 state 不做修改。
        //          handleStreamStart 应先于 tool_start 到达，如果 tool_start 先到达说明时序异常。
        // 影响范围: 工具调用与流式消息的时序竞争场景
        // 修复日期: 2026-05-13
        logger.warn(
          '消息不存在，跳过工具调用添加 | messageId:',
          messageId,
          'toolCall:',
          toolCall.call_id,
        )
        return state
      }

      const message = pipelineMessages[messageIndex]
      const existingToolCalls = message.toolCalls || []

      if (existingToolCalls.some((tc) => tc.call_id === toolCall.call_id)) {
        return state
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: [...existingToolCalls, toolCall],
        // BUG-FIX: 如果消息没有 parentId 但传入了 parentId，更新它
        ...(parentId && !message.parentId ? { parentId } : {}),
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /**
   * 更新指定管道中消息的工具调用
   */
  updateToolCallInMessage: (
    pipelineId: string,
    messageId: string,
    callId: string,
    updates: Partial<MessageToolCall>,
  ) => {
    usePipelineMessageStore.setState((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = pipelineMessages[messageIndex]
      const toolCalls = message.toolCalls || []
      const toolCallIndex = toolCalls.findIndex((tc) => tc.call_id === callId)

      if (toolCallIndex < 0) {
        const newToolCall: MessageToolCall = {
          call_id: callId,
          tool_name: ((updates as Record<string, unknown>).tool_name as string) || callId,
          tool_args:
            ((updates as Record<string, unknown>).tool_args as Record<string, unknown>) || {},
          status:
            ((updates as Record<string, unknown>)
              .status as ToolCallStatus) || 'completed',
          result: (updates as Record<string, unknown>).result,
          error: (updates as Record<string, unknown>).error as string | undefined,
          duration_ms: (updates as Record<string, unknown>).duration_ms as number | undefined,
          completed_at: new Date().toISOString(),
        }
        const updatedMessages = [...pipelineMessages]
        updatedMessages[messageIndex] = {
          ...message,
          toolCalls: [...toolCalls, newToolCall],
        }
        return {
          messagesByPipeline: {
            ...state.messagesByPipeline,
            [pipelineId]: updatedMessages,
          },
        }
      }

      const updatedToolCalls = [...toolCalls]
      updatedToolCalls[toolCallIndex] = {
        ...updatedToolCalls[toolCallIndex],
        ...updates,
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: updatedToolCalls,
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /**
   * 更新指定管道中消息的工具调用进度
   */
  updateToolCallProgress: (
    pipelineId: string,
    messageId: string,
    toolCallId: string,
    progress: number,
    currentStep?: string,
    estimatedRemainingMs?: number,
  ) => {
    usePipelineMessageStore.setState((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = pipelineMessages[messageIndex]
      const toolCalls = message.toolCalls || []
      const toolCallIndex = toolCalls.findIndex((tc) => tc.call_id === toolCallId)

      if (toolCallIndex < 0) {
        return state
      }

      const updatedToolCalls = [...toolCalls]
      updatedToolCalls[toolCallIndex] = {
        ...updatedToolCalls[toolCallIndex],
        progress: Math.min(100, Math.max(0, progress)),
        currentStep,
        estimatedRemainingMs,
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: updatedToolCalls,
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /**
   * 追加工具调用输出到指定管道的消息
   */
  appendToolCallOutput: (
    pipelineId: string,
    messageId: string,
    toolCallId: string,
    output: string,
  ) => {
    usePipelineMessageStore.setState((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = pipelineMessages[messageIndex]
      const toolCalls = message.toolCalls || []
      const toolCallIndex = toolCalls.findIndex((tc) => tc.call_id === toolCallId)

      if (toolCallIndex < 0) {
        return state
      }

      const updatedToolCalls = [...toolCalls]
      const existingPartialOutput = updatedToolCalls[toolCallIndex].partialOutput || []
      updatedToolCalls[toolCallIndex] = {
        ...updatedToolCalls[toolCallIndex],
        partialOutput: [...existingPartialOutput, output],
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: updatedToolCalls,
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },
}))
