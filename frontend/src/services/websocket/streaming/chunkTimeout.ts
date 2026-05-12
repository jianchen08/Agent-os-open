/**
 * 流式块超时检测（页面可见性感知）
 */
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

const _debugLogger = loggers.websocket

/** 超时间隔常量（60秒） */
export const CHUNK_INTERVAL_TIMEOUT_MS = 60_000

interface ChunkTimeoutEntry {
  timer: ReturnType<typeof setTimeout>
  messageId: string
  /** 计时器启动时的单调时间戳（毫秒），用于页面后台期间暂停计时 */
  startedAt: number
  /** 页面可见时剩余的超时毫秒数 */
  remainingMs: number
  /** 是否处于暂停状态（页面不可见时暂停） */
  paused: boolean
  /** 暂停时记录的时间戳，用于计算已消耗时间 */
  pausedAt: number
}

const _chunkTimeoutMap: Map<string, ChunkTimeoutEntry> = new Map()

/**
 * chunk 超时回调：标记流式响应中断
 */
function _onChunkTimeout(pipelineId: string): void {
  const entry = _chunkTimeoutMap.get(pipelineId)
  if (!entry) return
  _chunkTimeoutMap.delete(pipelineId)
  useStreamingStore.getState().stopStreamingForTab(pipelineId)
  pipelineStore.getState().updateMessage(pipelineId, entry.messageId, {
    content: '\n\n⚠️ 流式响应中断，请重试。',
    status: 'error',
  } as any)

  useNotificationStore.getState().addNotification({
    title: '响应中断',
    message: '流式响应超时中断，请重新发送消息',
    priority: 'high',
    category: 'error',
    isBlocking: false,
  })
}

/**
 * 重置流式块超时计时器
 *
 * 页面可见性感知：只在页面可见时才真正启动超时计时器，
 * 页面隐藏时暂停计时，恢复可见后继续倒计时。
 */
export function resetChunkTimeout(pipelineId: string, messageId: string): void {
  clearChunkTimeout(pipelineId)
  const now = performance.now()
  const isPageVisible = !document.hidden
  const entry: ChunkTimeoutEntry = {
    timer: null!,
    messageId,
    startedAt: now,
    remainingMs: CHUNK_INTERVAL_TIMEOUT_MS,
    paused: !isPageVisible,
    pausedAt: isPageVisible ? 0 : now,
  }
  if (isPageVisible) {
    entry.timer = setTimeout(() => _onChunkTimeout(pipelineId), CHUNK_INTERVAL_TIMEOUT_MS)
  }
  _chunkTimeoutMap.set(pipelineId, entry)
}

/**
 * 页面变为不可见时暂停所有 chunk 超时计时器
 */
function pauseAllChunkTimeouts(): void {
  const now = performance.now()
  for (const [pipelineId, entry] of _chunkTimeoutMap) {
    if (entry.paused) continue
    clearTimeout(entry.timer)
    const elapsed = now - entry.startedAt
    entry.remainingMs = Math.max(0, entry.remainingMs - elapsed)
    entry.paused = true
    entry.pausedAt = now
    _debugLogger.debug(`[CHUNK_TIMEOUT] 暂停: pipeline=%s remaining=%dms`, pipelineId.slice(0, 8), entry.remainingMs)
  }
}

/**
 * 页面恢复可见时恢复所有 chunk 超时计时器
 */
function resumeAllChunkTimeouts(): void {
  const now = performance.now()
  for (const [pipelineId, entry] of _chunkTimeoutMap) {
    if (!entry.paused) continue
    entry.paused = false
    entry.startedAt = now
    if (entry.remainingMs <= 0) {
      _onChunkTimeout(pipelineId)
    } else {
      entry.timer = setTimeout(() => _onChunkTimeout(pipelineId), entry.remainingMs)
      _debugLogger.debug(`[CHUNK_TIMEOUT] 恢复: pipeline=%s remaining=%dms`, pipelineId.slice(0, 8), entry.remainingMs)
    }
  }
}

/**
 * 清除指定管道的超时计时器
 */
export function clearChunkTimeout(pipelineId: string): void {
  const entry = _chunkTimeoutMap.get(pipelineId)
  if (entry) {
    clearTimeout(entry.timer)
    _chunkTimeoutMap.delete(pipelineId)
  }
}

/**
 * 清除所有超时计时器
 */
export function clearAllChunkTimeouts(): void {
  for (const [, entry] of _chunkTimeoutMap) clearTimeout(entry.timer)
  _chunkTimeoutMap.clear()
}

/**
 * 获取指定管道的超时计时器对应的 messageId（用于 keepalive 场景）
 */
export function getChunkTimeoutMessageId(pipelineId: string): string | null {
  return _chunkTimeoutMap.get(pipelineId)?.messageId ?? null
}

/**
 * 页面可见性变化处理函数
 */
function _handleVisibilityChange(): void {
  if (document.hidden) {
    pauseAllChunkTimeouts()
  } else {
    resumeAllChunkTimeouts()
  }
}

/**
 * 初始化页面可见性监听器
 */
export function initVisibilityListener(): void {
  document.addEventListener('visibilitychange', _handleVisibilityChange)
}

/**
 * 销毁页面可见性监听器
 */
export function destroyVisibilityListener(): void {
  document.removeEventListener('visibilitychange', _handleVisibilityChange)
}
