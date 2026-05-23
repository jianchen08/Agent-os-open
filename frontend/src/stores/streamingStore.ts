import { create } from 'zustand'
import { usePipelineMessageStore } from './pipelineMessageStore'
import type { MessageToolCall, ThinkingStep } from '@/types/models'

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
   * 停止所有 streaming 状态，清理 streamingTabs 和 refreshingMessageId
   * Part 状态由 finalizeMessage 统一处理
   */
  stopStreaming: () => {
    set({ isStreaming: false, refreshingMessageId: null, streamingTabs: {} })
  },

  /**
   * 结束指定管道中消息的思考状态，将 thinking Part 标记为 done
   */
  endThinking: (pipelineId: string, messageId: string, durationMs?: number) => {
    const store = usePipelineMessageStore.getState()
    const partIndex = store.findLastPartIndex(pipelineId, messageId, 'thinking')
    if (partIndex >= 0) {
      store.updatePart(pipelineId, messageId, partIndex, {
        state: 'done',
        durationMs,
      } as any)
    }
  },

  /**
   * 更新指定管道中消息的思考步骤，通过 Parts 体系更新 thinking Part 的 steps
   */
  updateThinkingStep: (pipelineId: string, messageId: string, step: ThinkingStep) => {
    const store = usePipelineMessageStore.getState()
    const partIndex = store.findLastPartIndex(pipelineId, messageId, 'thinking')
    if (partIndex >= 0) {
      const msg = store.messagesByPipeline[pipelineId]?.find((m) => m.id === messageId)
      const part = msg?.parts?.[partIndex] as any
      if (part) {
        const steps = part.steps || []
        const stepIdx = steps.findIndex((s: any) => s.id === step.id)
        const updatedSteps = stepIdx >= 0
          ? steps.map((s: any, i: number) => (i === stepIdx ? step : s))
          : [...steps, step]
        store.updatePart(pipelineId, messageId, partIndex, {
          steps: updatedSteps,
          currentStepIndex: updatedSteps.length - 1,
        } as any)
      }
    }
  },

  /**
   * 向指定管道的消息追加工具调用 Part
   */
  addToolCallToMessage: (
    pipelineId: string,
    messageId: string,
    toolCall: MessageToolCall,
    parentId?: string,
  ) => {
    const store = usePipelineMessageStore.getState()
    store.appendPart(pipelineId, messageId, {
      type: 'tool_call',
      callId: toolCall.call_id || (toolCall as any).id || '',
      name: toolCall.tool_name || (toolCall as any).name || '',
      args: toolCall.tool_args || (toolCall as any).args || {},
      state: toolCall.status === 'completed' ? 'done' : toolCall.status === 'failed' ? 'error' : 'calling',
      sequence: toolCall.sequence ?? Date.now(),
    } as any)
  },

  /**
   * 更新指定管道中消息的工具调用 Part 状态
   */
  updateToolCallInMessage: (
    pipelineId: string,
    messageId: string,
    callId: string,
    updates: Partial<MessageToolCall>,
  ) => {
    const store = usePipelineMessageStore.getState()
    const partIndex = store.findToolCallPartIndex(pipelineId, messageId, callId)
    if (partIndex >= 0) {
      store.updatePart(pipelineId, messageId, partIndex, {
        state: updates.status === 'completed' ? 'done' : updates.status === 'failed' ? 'error' : 'calling',
        result: updates.result,
        error: updates.error,
        durationMs: updates.duration_ms,
      } as any)
    }
  },

  /**
   * 更新指定管道中消息的工具调用进度，通过 Parts 体系更新 tool_call Part
   */
  updateToolCallProgress: (
    pipelineId: string,
    messageId: string,
    toolCallId: string,
    progress: number,
    currentStep?: string,
    estimatedRemainingMs?: number,
  ) => {
    const store = usePipelineMessageStore.getState()
    const partIndex = store.findToolCallPartIndex(pipelineId, messageId, toolCallId)
    if (partIndex >= 0) {
      store.updatePart(pipelineId, messageId, partIndex, {
        progress: Math.min(100, Math.max(0, progress)),
        currentStep,
        estimatedRemainingMs,
      } as any)
    }
  },

  /**
   * 追加工具调用输出到指定管道消息的 tool_call Part
   */
  appendToolCallOutput: (
    pipelineId: string,
    messageId: string,
    toolCallId: string,
    output: string,
  ) => {
    const store = usePipelineMessageStore.getState()
    const partIndex = store.findToolCallPartIndex(pipelineId, messageId, toolCallId)
    if (partIndex >= 0) {
      const msg = store.messagesByPipeline[pipelineId]?.find((m) => m.id === messageId)
      const part = msg?.parts?.[partIndex] as any
      const existingPartialOutput = part?.partialOutput || []
      store.updatePart(pipelineId, messageId, partIndex, {
        partialOutput: [...existingPartialOutput, output],
      } as any)
    }
  },
}))
