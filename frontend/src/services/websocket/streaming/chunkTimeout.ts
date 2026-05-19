/**
 * 流式块超时检测
 *
 * BUG-FIX-fix_20260513_chunk_timeout_false_alarm:
 * 问题根因: 页面可见性暂停/恢复机制导致误触发超时。当页面隐藏时，浏览器可能延迟
 *          处理 WebSocket 事件（JS 事件循环节流），而 visibilitychange 的 resume
 *          回调先于排队的 WS 事件执行，导致 remainingMs<=0 时直接触发超时。
 *          即使事件到达了，wrapper 的 resetChunkTimeout 也会因为浏览器节流而
 *          不被及时执行，最终 resumeAllChunkTimeouts 使用过期的 remainingMs 触发误报。
 * 修复方案: 移除页面可见性暂停/恢复机制，计时器始终正常运行。
 *          只要后端持续发送事件（chunk / keepalive 等），集中式 wrapper 就会重置计时器。
 *          如果真的 60 秒无任何事件，超时是合理的。
 */

/** 超时间隔常量（60秒） */
export const CHUNK_INTERVAL_TIMEOUT_MS = 60_000

/**
 * 等待 stream_start 的超时时间（45秒）
 *
 * BUG-FIX-fix_20260512_pending_stream_timeout:
 * 问题根因: 用户发送消息后，如果后端 LLM API 卡住（如智谱 glm-5.1 无响应），
 *          后端不会发送 stream_start 事件，前端也没有超时检测，
 *          导致前端一直处于"等待"状态，用户无法得到任何反馈。
 * 修复方案: 发送消息后启动 pending 超时计时器，如果在 45 秒内没有收到
 *          stream_start（或任何流式事件），自动标记为超时并通知用户。
 *          收到 stream_start 时自动清除该计时器。
 */
export const PENDING_STREAM_TIMEOUT_MS = 15_000

interface ChunkTimeoutEntry {
  timer: ReturnType<typeof setTimeout>
  messageId: string
}

const _chunkTimeoutMap: Map<string, ChunkTimeoutEntry> = new Map()

interface PendingStreamEntry {
  timer: ReturnType<typeof setTimeout>
  sessionId: string
}

const _pendingStreamMap: Map<string, PendingStreamEntry> = new Map()

/**
 * 超时回调类型：超时触发时通知上层进行状态清理
 */
type ChunkTimeoutCallback = (data: { pipelineId: string; messageId: string }) => void

/**
 * 超时回调注册表（由 streaming/index.ts 注册）
 *
 * BUG-FIX-fix_20260513_chunk_timeout_cleanup:
 * 问题根因: 超时后只打 console.debug，不通知上层，导致用户无反馈、streaming 状态残留。
 * 修复方案: 通过回调注册机制，由 streaming handler 注册超时回调，统一处理状态清理。
 * 影响范围: 流式响应超时后的用户体验
 * 修复日期: 2026-05-13
 */
let _chunkTimeoutCallback: ChunkTimeoutCallback | null = null

/**
 * 注册超时回调（由 streaming/index.ts 调用）
 *
 * chunkTimeout 只负责超时检测和通知，不直接操作 store，保持职责分离。
 */
export function onChunkTimeout(callback: ChunkTimeoutCallback): void {
  _chunkTimeoutCallback = callback
}

/**
 * pending stream 超时回调：后端长时间未响应
 *
 * BUG-FIX-fix_20260513_chunk_timeout_cleanup:
 * 问题根因: 超时后只打 console.debug，不通知上层，导致用户无反馈、streaming 状态残留。
 * 修复方案: 通过回调通知上层处理超时状态清理。
 * 影响范围: 后端无响应时的用户体验
 * 修复日期: 2026-05-13
 */
function _onPendingStreamTimeout(pipelineId: string): void {
  const entry = _pendingStreamMap.get(pipelineId)
  if (!entry) return
  _pendingStreamMap.delete(pipelineId)
  console.debug(
    `[chunkTimeout] pending stream 超时: pipelineId=${pipelineId}, 后端 ${PENDING_STREAM_TIMEOUT_MS / 1000}s 未发送 stream_start`,
  )
  // 通知上层处理超时状态清理
  _chunkTimeoutCallback?.({ pipelineId, messageId: '' })
}

/**
 * 启动"等待 stream_start"的超时计时器
 *
 * 用户发送消息后调用。如果在 PENDING_STREAM_TIMEOUT_MS 内没有收到
 * stream_start 事件（通过 clearPendingStreamTimeout 清除），
 * 则弹出超时通知提醒用户。
 */
export function startPendingStreamTimeout(pipelineId: string, sessionId: string): void {
  clearPendingStreamTimeout(pipelineId)
  const entry: PendingStreamEntry = {
    timer: setTimeout(() => _onPendingStreamTimeout(pipelineId), PENDING_STREAM_TIMEOUT_MS),
    sessionId,
  }
  _pendingStreamMap.set(pipelineId, entry)
}

/**
 * 清除 pending stream 超时计时器（收到 stream_start 时调用）
 */
export function clearPendingStreamTimeout(pipelineId: string): void {
  const entry = _pendingStreamMap.get(pipelineId)
  if (entry) {
    clearTimeout(entry.timer)
    _pendingStreamMap.delete(pipelineId)
  }
}

/**
 * chunk 超时回调：标记流式响应中断
 *
 * BUG-FIX-fix_20260513_chunk_timeout_cleanup:
 * 问题根因: 超时后只打 console.debug，不通知上层，导致用户无反馈、streaming 状态残留。
 * 修复方案: 通过回调通知上层处理超时状态清理。
 * 影响范围: 流式响应超时后的用户体验
 * 修复日期: 2026-05-13
 */
function _onChunkTimeout(pipelineId: string): void {
  const entry = _chunkTimeoutMap.get(pipelineId)
  if (!entry) return
  _chunkTimeoutMap.delete(pipelineId)
  console.debug(
    `[chunkTimeout] chunk 间隔超时: pipelineId=${pipelineId}, messageId=${entry.messageId}, ${CHUNK_INTERVAL_TIMEOUT_MS / 1000}s 未收到任何流式事件`,
  )
  // 通知上层处理超时状态清理
  _chunkTimeoutCallback?.({ pipelineId, messageId: entry.messageId })
}

/**
 * 重置流式块超时计时器
 *
 * 每次收到非终止流式事件时调用，重新开始 60 秒倒计时。
 */
export function resetChunkTimeout(pipelineId: string, messageId: string): void {
  clearChunkTimeout(pipelineId)
  const entry: ChunkTimeoutEntry = {
    timer: setTimeout(() => _onChunkTimeout(pipelineId), CHUNK_INTERVAL_TIMEOUT_MS),
    messageId,
  }
  _chunkTimeoutMap.set(pipelineId, entry)
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
  for (const [, entry] of _pendingStreamMap) clearTimeout(entry.timer)
  _pendingStreamMap.clear()
}

/**
 * 获取指定管道的超时计时器对应的 messageId（用于 keepalive 场景）
 */
export function getChunkTimeoutMessageId(pipelineId: string): string | null {
  return _chunkTimeoutMap.get(pipelineId)?.messageId ?? null
}
