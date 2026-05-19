/**
 * streamingStore 测试 - per-tab streaming 状态管理
 *
 * 验证：
 * - setStreamingForTab 正确设置/清除 tab streaming 状态
 * - isTabStreaming 查询正确
 * - stopStreamingForTab 结束指定 tab
 * - stopStreaming 清理所有状态
 * - isStreaming 聚合状态正确
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// Mock pipelineMessageStore（streamingStore 依赖它）
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: {
    getState: () => ({
      messagesByPipeline: {},
      setState: vi.fn(),
    }),
    setState: vi.fn(),
  },
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  },
}))

describe('streamingStore - per-tab streaming 状态管理', () => {
  let useStreamingStore: typeof import('../streamingStore').useStreamingStore

  beforeEach(async () => {
    vi.resetModules()
    // 重新导入以获取干净 store
    const mod = await import('../streamingStore')
    useStreamingStore = mod.useStreamingStore
    // 重置状态
    useStreamingStore.setState({
      isStreaming: false,
      refreshingMessageId: null,
      streamingTabs: {},
    })
  })

  describe('setStreamingForTab', () => {
    it('设置 tab 为 streaming 后 isTabStreaming 返回 true', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)

      expect(useStreamingStore.getState().isTabStreaming('tab-1')).toBe(true)
    })

    it('未设置的 tab 返回 false', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)

      expect(useStreamingStore.getState().isTabStreaming('tab-2')).toBe(false)
    })

    it('设置 streaming 后全局 isStreaming 为 true', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)

      expect(useStreamingStore.getState().isStreaming).toBe(true)
    })

    it('清除所有 tab streaming 后 isStreaming 恢复 false', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)
      store.setStreamingForTab('tab-1', false)

      expect(useStreamingStore.getState().isStreaming).toBe(false)
    })

    it('多个 tab 中移除一个后 isStreaming 仍为 true', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)
      store.setStreamingForTab('tab-2', true)

      store.setStreamingForTab('tab-1', false)

      expect(useStreamingStore.getState().isStreaming).toBe(true)
      expect(useStreamingStore.getState().isTabStreaming('tab-2')).toBe(true)
    })

    it('重复设置相同状态不触发多余更新', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)
      const stateBefore = useStreamingStore.getState().streamingTabs

      store.setStreamingForTab('tab-1', true)
      const stateAfter = useStreamingStore.getState().streamingTabs

      // 状态不应变化
      expect(stateBefore).toEqual(stateAfter)
    })
  })

  describe('stopStreamingForTab', () => {
    it('正确结束指定 tab 的 streaming', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)

      store.stopStreamingForTab('tab-1')

      expect(useStreamingStore.getState().isTabStreaming('tab-1')).toBe(false)
    })
  })

  describe('stopStreaming', () => {
    it('清除所有 streaming 状态', () => {
      const store = useStreamingStore.getState()
      store.setStreamingForTab('tab-1', true)
      store.setStreamingForTab('tab-2', true)
      store.setRefreshingMessageId('msg-1')

      store.stopStreaming()

      const state = useStreamingStore.getState()
      expect(state.isStreaming).toBe(false)
      expect(state.refreshingMessageId).toBeNull()
      expect(state.streamingTabs).toEqual({})
    })
  })

  describe('setRefreshingMessageId', () => {
    it('正确设置和清除 refreshingMessageId', () => {
      const store = useStreamingStore.getState()

      store.setRefreshingMessageId('msg-1')
      expect(useStreamingStore.getState().refreshingMessageId).toBe('msg-1')

      store.setRefreshingMessageId(null)
      expect(useStreamingStore.getState().refreshingMessageId).toBeNull()
    })
  })
})
