/**
 * 流式块超时检测
 *
 * 单一超时机制：120 秒内未收到任何流式事件则触发超时。
 */

/** 超时间隔常量（120秒） */
export const CHUNK_INTERVAL_TIMEOUT_MS = 120_000

interface ChunkTimeoutEntry {
  timer: ReturnType<typeof setTimeout>
  messageId: string
}

const _timeoutMap: Map<string, ChunkTimeoutEntry> = new Map()

type ChunkTimeoutCallback = (data: { pipelineId: string; messageId: string }) => void

let _chunkTimeoutCallback: ChunkTimeoutCallback | null = null

export function onChunkTimeout(callback: ChunkTimeoutCallback): void {
  _chunkTimeoutCallback = callback
}

function _onChunkTimeout(pipelineId: string): void {
  const entry = _timeoutMap.get(pipelineId)
  if (!entry) return
  _timeoutMap.delete(pipelineId)
  console.debug(
    `[chunkTimeout] chunk 间隔超时: pipelineId=${pipelineId}, messageId=${entry.messageId}, ${CHUNK_INTERVAL_TIMEOUT_MS / 1000}s 未收到任何流式事件`,
  )
  _chunkTimeoutCallback?.({ pipelineId, messageId: entry.messageId })
}

export function resetChunkTimeout(pipelineId: string, messageId: string): void {
  clearChunkTimeout(pipelineId)
  const entry: ChunkTimeoutEntry = {
    timer: setTimeout(() => _onChunkTimeout(pipelineId), CHUNK_INTERVAL_TIMEOUT_MS),
    messageId,
  }
  _timeoutMap.set(pipelineId, entry)
}

export function clearChunkTimeout(pipelineId: string): void {
  const entry = _timeoutMap.get(pipelineId)
  if (entry) {
    clearTimeout(entry.timer)
    _timeoutMap.delete(pipelineId)
  }
}

export function clearAllChunkTimeouts(): void {
  for (const [, entry] of _timeoutMap) clearTimeout(entry.timer)
  _timeoutMap.clear()
}

export function getChunkTimeoutMessageId(pipelineId: string): string | null {
  return _timeoutMap.get(pipelineId)?.messageId ?? null
}
