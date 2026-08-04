/**
 * 迭代事件处理器（管道引擎迭代开始/结束时后端发送的 iteration 类型事件）
 *
 * 后端 stream_bridge.py 在 L317-328 发送 iteration 事件，
 * 携带 iteration（当前迭代序号）和 max_iterations（最大迭代次数）。
 * 前端仅做日志记录，不写入 parts[]（iteration 信息为日志性质，无需渲染）。
 */
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/**
 * 处理迭代事件
 *
 * 迭代信息为日志性质，仅记录 debug 日志，不操作消息的 parts[]。
 */
export function handleIteration(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    _debugLogger.warn(
      `[ITERATION] pipeline_id missing, _threadId=%s msgId=%s`,
      eventData.data?._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
    )
    return
  }
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  const iteration = eventData.iteration ?? eventData.data?.iteration ?? 0
  const maxIterations = eventData.max_iterations ?? eventData.data?.max_iterations ?? 0

  _debugLogger.debug(
    `[ITERATION] pipeline=%s msgId=%s iter=%d/%d`,
    pipelineId?.slice(0, 12), messageId?.slice(0, 12), iteration, maxIterations,
  )
}
