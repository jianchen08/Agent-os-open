import { create } from 'zustand'

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
}

export const useStreamingStore = create<StreamingState>()((set, get) => ({
  isStreaming: false,
  refreshingMessageId: null,
  streamingTabs: {},

  /** BUG-FIX-fix_20260526_fp_s2: 为指定标签页设置 streaming 状态 */
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

  /** BUG-FIX-fix_20260526_fp_s2: 查询指定标签页是否正在 streaming */
  isTabStreaming: (tabId: string) => {
    return get().streamingTabs[tabId] ?? false
  },

  /** BUG-FIX-fix_20260526_fp_s2: 结束指定标签页的 streaming 状态 */
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
}))
