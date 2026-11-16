/** @feature 跨轮消息归属 | @ci: frontend-test */
/**
 * 跨轮/跨 run 残影回归（2026-10-29 用户实证）：
 * 「上一轮的 AI 消息和工具调用渲染到下一轮/下一个气泡里」。
 *
 * 断行为：
 * 1. 上一轮消息仍有未解析 tool_call（state=calling）时，下一轮 stream_start
 *    不得删除该消息——否则迟到的 tool_result 找不到声明卡，会 FIXUP 到事件
 *    携带的下一轮 message_id，工具卡就长在新气泡里。
 * 2. tool_result 归并必须落在声明卡所在消息（上一轮），不得落到下一轮。
 * 3. 新 run（用户新消息）同样适用：上一 run 的未完成工具卡不得漂移到新气泡。
 * 4. 正常路径（工具卡已 done）不回归：上一轮消息保留为 completed。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

vi.mock('@/stores/pipelineRegistryStore', () => ({
  usePipelineRegistryStore: {
    getState: () => ({ applyStreamStatus: vi.fn() }),
  },
}))

const PIPELINE_ID = 'pipe-cross-round-residue'
const THREAD_ID = 'thread-cross-round-residue'

function ev(type: string, messageId: string, data: Record<string, any> = {}) {
  return {
    type,
    data: {
      pipeline_id: PIPELINE_ID,
      _threadId: THREAD_ID,
      message_id: messageId,
      ...data,
    },
  }
}

describe('跨轮消息归属：上一轮内容不得漂移到下一轮气泡', () => {
  let store: any
  let h: any

  beforeEach(async () => {
    vi.useFakeTimers()
    store = await resetStreamingStore(PIPELINE_ID, THREAD_ID, PIPELINE_ID)
    h = await import('@/services/websocket/streaming/handlers')
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  const messages = () => store.getState().getMessages(PIPELINE_ID)
  const byId = (id: string) => messages().find((m: any) => m.id === id)
  const toolParts = (id: string) =>
    (byId(id)?.parts || []).filter((p: any) => p.type === 'tool_call')
  const textContent = (id: string) =>
    ((byId(id)?.parts || []) as any[])
      .filter((p) => p.type === 'text')
      .map((p) => p.content || '')
      .join('')

  /** 构造上一轮：文本 + 未完成的工具卡（工具还在执行）。 */
  function seedPreviousRoundWithRunningTool(roundId: string, callId: string, text: string) {
    h.handleStreamStart(ev('stream_start', roundId))
    h.handleBlockStart(ev('block_start', roundId, { index: 0, block_type: 'text' }))
    h.handleTextDelta(ev('text_delta', roundId, { index: 0, text }))
    h.handleBlockEnd(ev('block_end', roundId, { index: 0, block: { block_type: 'text' } }))
    h.handleToolStart(
      ev('tool_start', roundId, { call_id: callId, tool_name: 'bash', args: { cmd: 'sleep 30' } }),
    )
  }

  it('同一 run 下一轮：未完成工具卡的消息必须保留，迟到结果归并回上一轮', () => {
    seedPreviousRoundWithRunningTool('a_prev', 'call_1', '上一轮的正文')

    // 下一轮 LLM 开始：当前实现会把带 calling 工具卡的 a_prev 直接删除
    h.handleStreamStart(ev('stream_start', 'a_next'))

    expect(byId('a_prev'), '上一轮消息不得被下一轮 stream_start 删除').toBeTruthy()
    expect(textContent('a_prev'), '上一轮正文必须留在上一轮').toContain('上一轮的正文')
    expect(toolParts('a_prev').map((p: any) => p.callId), '上一轮工具卡必须留在上一轮').toEqual([
      'call_1',
    ])

    // 工具结果迟到，契约事件 message_id 指向下一轮（已知「晚一轮」形态）
    h.handleToolResult(
      ev('tool_result', 'a_next', { call_id: 'call_1', tool_name: 'bash', success: true, result: 'ok' }),
    )

    const prevTool = toolParts('a_prev').find((p: any) => p.callId === 'call_1')
    expect(prevTool?.state, '迟到结果必须归并到声明卡（上一轮）').toBe('done')
    expect(toolParts('a_next'), '下一轮气泡不得出现上一轮的工具卡').toEqual([])
    expect(textContent('a_next'), '下一轮正文不得混入上一轮内容').not.toContain('上一轮的正文')
  })

  it('跨 run（用户发新消息）：上一 run 的未完成工具卡不得漂移到新气泡', () => {
    seedPreviousRoundWithRunningTool('a_old_run', 'call_old', '旧 run 的正文')

    // 用户发新消息 → 新 run 的第一轮 stream_start
    h.handleStreamStart(ev('stream_start', 'a_new_run'))

    expect(byId('a_old_run'), '旧 run 消息不得被新 run 的 stream_start 删除').toBeTruthy()
    expect(toolParts('a_old_run').map((p: any) => p.callId)).toEqual(['call_old'])

    h.handleToolResult(
      ev('tool_result', 'a_new_run', {
        call_id: 'call_old',
        tool_name: 'bash',
        success: true,
        result: 'old-ok',
      }),
    )

    expect(toolParts('a_new_run'), '新 run 气泡不得出现旧 run 的工具卡').toEqual([])
    const oldTool = toolParts('a_old_run').find((p: any) => p.callId === 'call_old')
    expect(oldTool?.state, '旧工具结果必须归并回旧 run 消息').toBe('done')
  })

  it('正常路径不回归：工具卡已 done 的上一轮消息保留为 completed，下一轮独立', () => {
    seedPreviousRoundWithRunningTool('a_done', 'call_done', '已完成轮正文')
    h.handleToolResult(
      ev('tool_result', 'a_done', {
        call_id: 'call_done',
        tool_name: 'bash',
        success: true,
        result: 'ok',
      }),
    )

    h.handleStreamStart(ev('stream_start', 'a_after'))

    const prev = byId('a_done')
    expect(prev, '已收工具的上一轮消息必须保留').toBeTruthy()
    expect(prev.status, '上一轮消息应标 completed').toBe('completed')
    expect(toolParts('a_done')[0]?.state).toBe('done')
    expect(toolParts('a_after'), '下一轮不得继承上一轮工具卡').toEqual([])
  })
})
