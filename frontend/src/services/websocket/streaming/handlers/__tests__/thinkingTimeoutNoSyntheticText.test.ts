/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * thinking 超时不写合成文案测试（2026-08-22 裁决）
 *
 * 反模式收口批次5：思考超时旧实现往真实消息的 thinking part 追加
 * "\n\n⏱ 思考超时，请尝试重新发送"——长思考/慢网被污染真实消息内容，
 * 且随 IndexedDB 持久化，恢复的 chunk 会接在假文案后；同时掩盖
 * thinking_end 丢失的契约断裂。
 *
 * 新行为：超时仅把 part 置为 done（保持可见可重试），不写任何合成内容。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

// streamHandler 的 RAF 批处理不参与本测试，mock 掉
vi.mock('../streamHandler', () => ({
  bufferChunk: vi.fn(),
  flushStreamChunkBuffer: vi.fn(),
}))

import { handleThinkingStart } from '../thinkingHandler'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'

function prepSeed(pipelineId: string, messageId: string) {
  pipelineStore.getState().addMessage(pipelineId, {
    id: messageId,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'streaming',
  })
}

beforeEach(() => {
  vi.useFakeTimers()
})

describe('thinking 超时收尾', () => {
  it('超时后 part 置 done 且不写合成文案（旧实现追加"思考超时"污染真实内容）', () => {
    const pid = 'P-thinking-test-1'
    const msgId = 'm-thinking-1'
    prepSeed(pid, msgId)

    handleThinkingStart({
      data: { pipeline_id: pid, message_id: msgId, _threadId: 'T1' },
    })
    expect(
      pipelineStore.getState().findLastPartIndex(pid, msgId, 'thinking'),
    ).toBe(0)

    // 推进 90s 超时
    vi.advanceTimersByTime(91_000)

    const part = pipelineStore.getState().getMessages(pid)[0]?.parts?.[0] as any
    expect(part).toBeDefined()
    expect(part.state).toBe('done')
    expect(part.content ?? '').not.toContain('⏱')
    expect(part.content ?? '').not.toContain('思考超时')
    // 未收到任何 chunk → 内容保持空（诚实状态，无假文案）
    expect(part.content ?? '').toBe('')
  })
})