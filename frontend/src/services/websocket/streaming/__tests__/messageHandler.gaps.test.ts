// @feature FP-T12 补测 | @ci frontend-test
/**
 * messageHandler 空内容警告双路补遗：
 * - 服务端完整形态合并结果为空（content 与 parts 均空）→ warn
 * - 旧 parts[] 兜底合并结果为空 → warn
 *
 * 走真实 pipelineMessageStore；logger 只 spy 不替换。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'
import { handleNewMessage } from '../handlers/messageHandler'

const PIPELINE = 'pipe-warn1a2b3c4d5e6f4789abcdef01234567'

function seedAssistant(): void {
  usePipelineMessageStore.getState().addMessage(PIPELINE, {
    id: 'msg-empty-target',
    sessionId: 'thread-1',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'streaming',
  } as never)
}

describe('handleNewMessage — 空内容合并 warn 双路', () => {
  beforeEach(() => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      streamingState: {},
      pipelines: {},
      pipelineSessionMap: {},
      activePipelineId: null,
    } as never)
    vi.spyOn(loggers.websocket, 'warn').mockClear()
  })

  it('服务端完整形态（data.message）合并后无内容 → warn（applyServerPayload 路）', () => {
    seedAssistant()
    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'msg-empty-target',
        message: { id: 'msg-empty-target', role: 'assistant', content: '', status: 'completed' },
      },
    } as never)

    expect(loggers.websocket.warn).toHaveBeenCalledWith(
      expect.stringContaining('[MSG_READY]'),
      'msg-empty-ta',
      PIPELINE.slice(0, 12),
    )
    const msg = usePipelineMessageStore.getState().getMessages(PIPELINE)[0]
    expect(msg.status).toBe('completed')
  })

  it('旧 parts[] 兜底形态合并后无内容 → warn（applyLegacyPartsMessage 路）', () => {
    seedAssistant()
    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'msg-empty-target',
        parts: [],
      },
    } as never)

    expect(loggers.websocket.warn).toHaveBeenCalledWith(
      expect.stringContaining('[MSG_READY]'),
      'msg-empty-ta',
      PIPELINE.slice(0, 12),
    )
  })

  it('有内容时不触发空内容 warn（防误报回归）', () => {
    seedAssistant()
    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'msg-empty-target',
        message: { id: 'msg-empty-target', role: 'assistant', content: '有内容', status: 'completed' },
      },
    } as never)
    expect(loggers.websocket.warn).not.toHaveBeenCalledWith(
      expect.stringContaining('[MSG_READY]'),
      expect.anything(),
      expect.anything(),
    )
  })
})
