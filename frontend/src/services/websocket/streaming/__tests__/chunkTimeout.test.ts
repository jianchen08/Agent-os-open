/**
 * chunkTimeout 测试 - 超时检测与回调通知
 *
 * 验证：
 * - resetChunkTimeout 启动计时器
 * - clearChunkTimeout 清除计时器
 * - 超时后触发回调通知
 * - 统一流式超时机制（pipeline_received → 120s → stream_start）
 * - clearAllChunkTimeouts 全量清理
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

describe('chunkTimeout', () => {
  let chunkTimeout: typeof import('../chunkTimeout')

  beforeEach(async () => {
    vi.resetModules()
    vi.useFakeTimers()
    chunkTimeout = await import('../chunkTimeout')
    chunkTimeout.clearAllChunkTimeouts()
  })

  afterEach(() => {
    chunkTimeout.clearAllChunkTimeouts()
    chunkTimeout.onChunkTimeout(() => {})
    vi.useRealTimers()
  })

  describe('resetChunkTimeout / clearChunkTimeout', () => {
    it('设置后 getChunkTimeoutMessageId 返回对应 messageId', () => {
      chunkTimeout.resetChunkTimeout('pipe-1', 'msg-1')
      expect(chunkTimeout.getChunkTimeoutMessageId('pipe-1')).toBe('msg-1')
    })

    it('clearChunkTimeout 后 messageId 返回 null', () => {
      chunkTimeout.resetChunkTimeout('pipe-1', 'msg-1')
      chunkTimeout.clearChunkTimeout('pipe-1')
      expect(chunkTimeout.getChunkTimeoutMessageId('pipe-1')).toBeNull()
    })

    it('未设置的 pipeline 返回 null', () => {
      expect(chunkTimeout.getChunkTimeoutMessageId('pipe-1')).toBeNull()
    })

    it('超时后触发回调并清理 entry', () => {
      const callback = vi.fn()
      chunkTimeout.onChunkTimeout(callback)
      chunkTimeout.resetChunkTimeout('pipe-1', 'msg-1')

      vi.advanceTimersByTime(chunkTimeout.CHUNK_INTERVAL_TIMEOUT_MS)

      expect(callback).toHaveBeenCalledWith({ pipelineId: 'pipe-1', messageId: 'msg-1' })
      expect(chunkTimeout.getChunkTimeoutMessageId('pipe-1')).toBeNull()
    })

    it('reset 重置后不触发旧计时器', () => {
      const callback = vi.fn()
      chunkTimeout.onChunkTimeout(callback)
      chunkTimeout.resetChunkTimeout('pipe-1', 'msg-1')
      chunkTimeout.resetChunkTimeout('pipe-1', 'msg-2')

      vi.advanceTimersByTime(chunkTimeout.CHUNK_INTERVAL_TIMEOUT_MS)

      expect(callback).toHaveBeenCalledTimes(1)
      expect(callback).toHaveBeenCalledWith({ pipelineId: 'pipe-1', messageId: 'msg-2' })
    })
  })

  describe('unified stream timeout', () => {
    it('超时后触发回调', () => {
      const callback = vi.fn()
      chunkTimeout.onChunkTimeout(callback)
      chunkTimeout.startUnifiedStreamTimeout('pipe-1', 'sess-1')

      vi.advanceTimersByTime(120_000)

      expect(callback).toHaveBeenCalledWith({ pipelineId: 'pipe-1', messageId: '' })
    })

    it('clearUnifiedStreamTimeout 阻止超时触发', () => {
      const callback = vi.fn()
      chunkTimeout.onChunkTimeout(callback)
      chunkTimeout.startUnifiedStreamTimeout('pipe-1', 'sess-1')
      chunkTimeout.clearUnifiedStreamTimeout('pipe-1')

      vi.advanceTimersByTime(120_000)

      expect(callback).not.toHaveBeenCalled()
    })
  })

  describe('clearAllChunkTimeouts', () => {
    it('清除所有计时器', () => {
      const callback = vi.fn()
      chunkTimeout.onChunkTimeout(callback)
      chunkTimeout.resetChunkTimeout('pipe-1', 'msg-1')
      chunkTimeout.resetChunkTimeout('pipe-2', 'msg-2')
      chunkTimeout.startUnifiedStreamTimeout('pipe-3', 'sess-1')

      chunkTimeout.clearAllChunkTimeouts()

      vi.advanceTimersByTime(chunkTimeout.CHUNK_INTERVAL_TIMEOUT_MS)
      vi.advanceTimersByTime(120_000)

      expect(callback).not.toHaveBeenCalled()
      expect(chunkTimeout.getChunkTimeoutMessageId('pipe-1')).toBeNull()
      expect(chunkTimeout.getChunkTimeoutMessageId('pipe-2')).toBeNull()
    })
  })
})
