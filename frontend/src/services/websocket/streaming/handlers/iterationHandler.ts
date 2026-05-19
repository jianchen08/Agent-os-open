/**
 * 迭代事件处理器（管道引擎迭代开始/结束时后端发送的 iteration 类型事件）
 *
 * 后端 stream_bridge.py 在 L317-328 发送 iteration 事件，
 * 携带 iteration（当前迭代序号）和 max_iterations（最大迭代次数）。
 * 前端将其作为 contentBlock 添加到当前 streaming 消息中，用于展示迭代进度。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/**
 * 处理迭代事件
 *
 * 将迭代信息作为 contentBlock 附加到当前 streaming 消息，
 * 前端渲染层可据此显示 "第 N/M 轮迭代" 等进度信息。
 */
export function handleIteration(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // FIX: pipeline_id 缺失时记录 warn
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

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const existingBlocks: any[] = msg.contentBlocks || []

  // 检查是否已有同序号的 iteration block，避免重复
  const alreadyExists = existingBlocks.some(
    (b: any) => b.type === 'iteration' && b.iteration === iteration,
  )
  if (alreadyExists) return

  const iterationBlock = {
    type: 'iteration' as const,
    iteration,
    max_iterations: maxIterations,
    sourceId: messageId,
  }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    contentBlocks: [...existingBlocks, iterationBlock],
  } as any)

  _debugLogger.debug(
    `[ITERATION] pipeline=%s msgId=%s iter=%d/%d`,
    pipelineId?.slice(0, 12), messageId?.slice(0, 12), iteration, maxIterations,
  )
}
