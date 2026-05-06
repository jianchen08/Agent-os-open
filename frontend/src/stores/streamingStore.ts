import { create } from 'zustand'
import { loggers } from '@/utils/logger'
import { useSessionStore } from './sessionStore'
import type { Message, MessageToolCall, ThinkingStep } from '@/types/models'

const logger = loggers.sessionStore

interface StreamingState {
  /** @deprecated 使用 isTabStreaming 替代，保留用于向后兼容 */
  isStreaming: boolean
  refreshingMessageId: string | null
  /** 每个标签页的 streaming 状态 (tabId -> isStreaming) */
  streamingTabs: Record<string, boolean>

  /** @deprecated 使用 setStreamingForTab 替代 */
  setStreaming: (isStreaming: boolean) => void
  setRefreshingMessageId: (messageId: string | null) => void
  /** 为指定标签页设置 streaming 状态 */
  setStreamingForTab: (tabId: string, isStreaming: boolean) => void
  /** 查询指定标签页是否正在 streaming */
  isTabStreaming: (tabId: string) => boolean
  /** 结束指定标签页的 streaming 状态 */
  stopStreamingForTab: (tabId: string) => void
  stopStreaming: () => void
  startThinking: (sessionId: string, messageId: string) => void
  updateThinkingContent: (sessionId: string, messageId: string, appendContent: string) => void
  endThinking: (sessionId: string, messageId: string, durationMs?: number) => void
  updateThinkingStep: (sessionId: string, messageId: string, step: ThinkingStep) => void
  addToolCallToMessage: (
    sessionId: string,
    messageId: string,
    toolCall: MessageToolCall,
    parentId?: string,
  ) => void
  updateToolCallInMessage: (
    sessionId: string,
    messageId: string,
    callId: string,
    updates: Partial<MessageToolCall>,
  ) => void
  updateToolCallProgress: (
    sessionId: string,
    messageId: string,
    toolCallId: string,
    progress: number,
    currentStep?: string,
    estimatedRemainingMs?: number,
  ) => void
  appendToolCallOutput: (
    sessionId: string,
    messageId: string,
    toolCallId: string,
    output: string,
  ) => void
}

export const useStreamingStore = create<StreamingState>()((set, get) => ({
  isStreaming: false,
  refreshingMessageId: null,
  streamingTabs: {},

  /** BUG-FIX-fix_20260506_per_tab_streaming: 全局 setStreaming 同步更新 streamingTabs */
  setStreaming: (isStreaming: boolean) => {
    const currentValue = get().isStreaming
    if (currentValue !== isStreaming) {
      set({ isStreaming })
    }
  },

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

  stopStreaming: () => {
    const sessionStore = useSessionStore.getState()
    const activeSessionId = sessionStore.activeSessionId
    let updatedMessages = sessionStore.messages

    if (activeSessionId) {
      const sessionMessages = sessionStore.messages[activeSessionId] || []
      const needsUpdate = sessionMessages.some(
        (m) => m.status === 'streaming' || m.thinking?.isThinking,
      )

      if (needsUpdate) {
        const clearedMessages = sessionMessages.map((message) => {
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

          if (Object.keys(updates).length > 0) {
            return { ...message, ...updates }
          }
          return message
        })

        updatedMessages = {
          ...sessionStore.messages,
          [activeSessionId]: clearedMessages,
        }
      }
    }

    useSessionStore.setState({ messages: updatedMessages })
    set({ isStreaming: false, refreshingMessageId: null, streamingTabs: {} })
  },

  startThinking: (sessionId: string, messageId: string) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      const updatedMessages = [...sessionMessages]

      if (messageIndex < 0) {
        updatedMessages.push({
          id: messageId,
          sessionId: sessionId,
          role: 'assistant',
          content: '',
          timestamp: new Date().toISOString(),
          thinking: {
            content: '',
            isThinking: true,
          },
        })
      } else {
        const existing = updatedMessages[messageIndex]
        updatedMessages[messageIndex] = {
          ...existing,
          thinking: {
            content: '',
            isThinking: true,
          },
        }
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  updateThinkingContent: (sessionId: string, messageId: string, appendContent: string) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      const updatedMessages = [...sessionMessages]

      if (messageIndex < 0) {
        updatedMessages.push({
          id: messageId,
          sessionId: sessionId,
          role: 'assistant',
          content: '',
          timestamp: new Date().toISOString(),
          thinking: {
            content: appendContent,
            isThinking: true,
          },
        })
      } else {
        const message = sessionMessages[messageIndex]
        const currentThinking = message.thinking || {
          content: '',
          isThinking: true,
        }
        const currentContent = currentThinking.content

        let newContent: string
        let skipReason: string | null = null

        if (appendContent.length === 0) {
          skipReason = '追加内容为空'
          newContent = currentContent
        } else if (appendContent === currentContent) {
          skipReason = '追加内容与当前内容完全相同'
          newContent = currentContent
        } else if (appendContent.startsWith(currentContent) && currentContent.length > 0) {
          const newPart = appendContent.substring(currentContent.length)
          if (newPart.length > 0) {
            logger.debug(
              '检测到累积内容，提取新增部分 | messageId:',
              messageId,
              '| 当前长度:',
              currentContent.length,
              '| 累积长度:',
              appendContent.length,
              '| 新增长度:',
              newPart.length,
            )
            newContent = appendContent
          } else {
            skipReason = '累积内容与当前内容相同，无新增部分'
            newContent = currentContent
          }
        } else if (currentContent.endsWith(appendContent)) {
          skipReason = '追加内容已存在于当前内容末尾'
          newContent = currentContent
        } else if (currentContent.includes(appendContent)) {
          skipReason = '追加内容已存在于当前内容中'
          newContent = currentContent
        } else {
          newContent = currentContent + appendContent
        }

        if (skipReason) {
          logger.debug(
            '跳过追加 | messageId:',
            messageId,
            '| 原因:',
            skipReason,
            '| 当前长度:',
            currentContent.length,
            '| 追加长度:',
            appendContent.length,
          )
        }

        updatedMessages[messageIndex] = {
          ...message,
          thinking: {
            ...currentThinking,
            content: newContent,
          },
        }
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  endThinking: (sessionId: string, messageId: string, durationMs?: number) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = sessionMessages[messageIndex]
      if (!message.thinking) {
        return state
      }

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...message,
        thinking: {
          ...message.thinking,
          isThinking: false,
          durationMs,
        },
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  updateThinkingStep: (sessionId: string, messageId: string, step: ThinkingStep) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = sessionMessages[messageIndex]
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

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...message,
        thinking: {
          ...thinking,
          steps: updatedSteps,
          currentStepIndex: updatedSteps.length - 1,
        },
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  addToolCallToMessage: (
    sessionId: string,
    messageId: string,
    toolCall: MessageToolCall,
    parentId?: string,
  ) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        logger.warn(
          '消息不存在，创建消息占位 | messageId:',
          messageId,
          'toolCall:',
          toolCall.call_id,
        )
        const newMessage: Message = {
          id: messageId,
          sessionId: sessionId,
          role: 'assistant',
          content: '',
          timestamp: new Date().toISOString(),
          toolCalls: [toolCall],
          // BUG-FIX: 设置 parentId，确保消息被正确的 tab 过滤
          parentId: parentId || null,
        }
        return {
          messages: {
            ...state.messages,
            [sessionId]: [...sessionMessages, newMessage],
          },
        }
      }

      const message = sessionMessages[messageIndex]
      const existingToolCalls = message.toolCalls || []

      if (existingToolCalls.some((tc) => tc.call_id === toolCall.call_id)) {
        return state
      }

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: [...existingToolCalls, toolCall],
        // BUG-FIX: 如果消息没有 parentId 但传入了 parentId，更新它
        ...(parentId && !message.parentId ? { parentId } : {}),
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  updateToolCallInMessage: (
    sessionId: string,
    messageId: string,
    callId: string,
    updates: Partial<MessageToolCall>,
  ) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = sessionMessages[messageIndex]
      const toolCalls = message.toolCalls || []
      const toolCallIndex = toolCalls.findIndex((tc) => tc.call_id === callId)

      if (toolCallIndex < 0) {
        const newToolCall: import('@/types/models').MessageToolCall = {
          call_id: callId,
          tool_name: ((updates as Record<string, unknown>).tool_name as string) || callId,
          tool_args:
            ((updates as Record<string, unknown>).tool_args as Record<string, unknown>) || {},
          status:
            ((updates as Record<string, unknown>)
              .status as import('@/types/models').ToolCallStatus) || 'completed',
          result: (updates as Record<string, unknown>).result,
          error: (updates as Record<string, unknown>).error as string | undefined,
          duration_ms: (updates as Record<string, unknown>).duration_ms as number | undefined,
          completed_at: new Date().toISOString(),
        }
        const updatedMessages = [...sessionMessages]
        updatedMessages[messageIndex] = {
          ...message,
          toolCalls: [...toolCalls, newToolCall],
        }
        return {
          messages: {
            ...state.messages,
            [sessionId]: updatedMessages,
          },
        }
      }

      const updatedToolCalls = [...toolCalls]
      updatedToolCalls[toolCallIndex] = {
        ...updatedToolCalls[toolCallIndex],
        ...updates,
      }

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: updatedToolCalls,
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  updateToolCallProgress: (
    sessionId: string,
    messageId: string,
    toolCallId: string,
    progress: number,
    currentStep?: string,
    estimatedRemainingMs?: number,
  ) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = sessionMessages[messageIndex]
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

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: updatedToolCalls,
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  appendToolCallOutput: (
    sessionId: string,
    messageId: string,
    toolCallId: string,
    output: string,
  ) => {
    useSessionStore.setState((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const message = sessionMessages[messageIndex]
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

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...message,
        toolCalls: updatedToolCalls,
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },
}))
