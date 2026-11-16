// @feature: FP-0.2.可观测性 工具卡直播面（流式事件归位） | @ci: frontend-test
// @ci: frontend-test
/**
 * 工具卡「执行期双卡 + 工具数据整体晚一轮」直播面回归测试（融合迭代管道）。
 *
 * 根因：融合迭代管道（tool_exec 步先于 llm 步、post 路由不写 core_type）下，
 * 第 N 轮 LLM 声明的工具调用在 N+1 轮迭代开头执行，tool_core 发出的
 * tool_start/tool_progress/tool_result 事件 message_id 全部 = a_{N+1}（晚一轮）。
 * 直接按 (pipelineId, messageId, callId) 寻址必然 miss → FIXUP 在 a_{N+1} 建
 * 野卡，a_N 的声明卡永远无数据（执行期同调用双卡）。
 *
 * 修复契约（ADR 2026-09-28-tool-call-live-card 单键归并的贯彻）：callId 是唯一
 * 稳定身份（provider id 或 call_<hex24>，跨消息唯一），消息只是容器——直接
 * 寻址 miss 时先同管道跨消息搜既有声明卡并归并到那里；全程无声明才走 FIXUP
 * 建卡兜底（事件真丢失自愈语义保持）。
 *
 * 贯穿性质断言：call_X 卡总数恒为 1；声明卡带数据非空壳；a_{N+1} 只有自己的卡。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'
import type * as sessionMod from '@/services/api/session'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<sessionMod>()
  return {
    ...actual,
    getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  }
})

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-round-shift-001'
const A1 = 'a_round_1'
const A2 = 'a_round_2'
const THREAD_ID = 'thread-round-shift-001'

const makeEvent = makeEventFactory(PIPELINE_ID, A1)

/** 后端 new_message.data.message 完整形态（toolCalls 无结果字段——工具尚未执行） */
function makeServerMessage(id: string, seq: number, calls: Array<{ id: string; name: string; args: string }>) {
  return {
    id,
    role: 'assistant',
    content: '',
    sequence: seq,
    reasoningContent: null,
    toolCalls: calls.map((c) => ({
      id: c.id, type: 'function', function: { name: c.name, arguments: c.args },
    })),
    timestamp: '2026-10-10T00:00:00Z',
    status: 'completed',
    thread_id: THREAD_ID,
  }
}

describe('工具契约事件 message_id 晚一轮：跨消息按 callId 归并声明卡', () => {
  let store: any
  let h: Record<string, any>

  beforeEach(async () => {
    await resetStreamingStore(PIPELINE_ID, THREAD_ID)
    store = (window as any).__pipelineStore
    const handlerMod = await import('@/services/websocket/streaming/handlers')
    h = handlerMod
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  /** 全管道所有 tool_call 卡（带宿主消息 id）——性质断言的计数面 */
  function allToolCards() {
    const msgs = store.getState().getMessages(PIPELINE_ID)
    return msgs.flatMap((m: any) =>
      ((m.parts || []) as any[])
        .filter((p) => p.type === 'tool_call')
        .map((p) => ({ msgId: m.id as string, ...p })),
    )
  }

  function cardsOf(callId: string) {
    return allToolCards().filter((p) => p.callId === callId)
  }

  it('场景1 主复现：call_X 声明于 a1、契约事件全挂 a2——归并回 a1 单卡贯穿终态，a2 无野卡', () => {
    // ===== 第 1 轮：LLM 声明 call_X（块协议输出即建卡）=====
    h.handleStreamStart(makeEvent('stream_start', {}))
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 0, text: '我来打包' }))
    h.handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 1, id: 'call_X', name: 'hello_pack', arguments_delta: '{"name":"世界"}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'tool_call', id: 'call_X', name: 'hello_pack', arguments: '{"name":"世界"}' } }))
    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0].msgId).toBe(A1)
    // 轮收尾（引擎轮模型：new_message 权威完整形态 + stream_end 末轮快照）
    h.handleNewMessage(makeEvent('new_message', {
      sequence: 3,
      message: makeServerMessage(A1, 3, [{ id: 'call_X', name: 'hello_pack', args: '{"name":"世界"}' }]),
    }))
    h.handleStreamEnd(makeEvent('stream_end', {
      final_sequence: 3,
      full_content: '我来打包',
      parts: [
        { type: 'text', content: '我来打包', state: 'done', sequence: 0 },
        { type: 'tool_call', callId: 'call_X', name: 'hello_pack', args: { name: '世界' }, state: 'calling', sequence: 1 },
      ],
    }))
    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0].msgId).toBe(A1)

    // ===== 第 2 轮：tool_exec 步先于 llm 步——契约事件 message_id 全部 = a2 =====
    h.handleStreamStart(makeEvent('stream_start', { message_id: A2 }))
    h.handleToolStart(makeEvent('tool_start', {
      message_id: A2, call_id: 'call_X', tool_name: 'hello_pack', args: { name: '世界' },
    }))
    // 核心断言：不建野卡，归并回 a1 声明卡（执行开始 calling + 全量 args）
    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0]).toMatchObject({ msgId: A1, state: 'calling', args: { name: '世界' } })

    h.handleToolProgress(makeEvent('tool_progress', {
      message_id: A2, call_id: 'call_X', tool_name: 'hello_pack', delta: 'packing...\n',
    }))
    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0].msgId).toBe(A1)
    expect(cardsOf('call_X')[0].partialOutput).toEqual(['packing...\n'])

    h.handleToolResult(makeEvent('tool_result', {
      message_id: A2, call_id: 'call_X', tool_name: 'hello_pack', result: '打包完成', success: true, duration_ms: 1200,
    }))
    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0]).toMatchObject({ msgId: A1, state: 'done', result: '打包完成', durationMs: 1200 })

    // a2 自己的 LLM 轮：声明 call_Y 并收尾
    h.handleBlockStart(makeEvent('block_start', { message_id: A2, index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { message_id: A2, index: 0, id: 'call_Y', name: 'next_tool', arguments_delta: '{"x":1}' }))
    h.handleBlockEnd(makeEvent('block_end', { message_id: A2, index: 0, block: { block_type: 'tool_call', id: 'call_Y', name: 'next_tool', arguments: '{"x":1}' } }))
    h.handleNewMessage(makeEvent('new_message', {
      message_id: A2,
      sequence: 4,
      message: makeServerMessage(A2, 4, [{ id: 'call_Y', name: 'next_tool', args: '{"x":1}' }]),
    }))

    // 终局性质：call_X 恰一卡且在 a1、带数据非空壳；a2 只有 call_Y
    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0]).toMatchObject({ msgId: A1, state: 'done', result: '打包完成', durationMs: 1200 })
    expect(cardsOf('call_X')[0].result).toBeDefined()
    const a2Cards = allToolCards().filter((p) => p.msgId === A2)
    expect(a2Cards).toHaveLength(1)
    expect(a2Cards[0].callId).toBe('call_Y')
  })

  it('场景2 并行工具双双错位：call_P/call_Q 声明于 a1、start/result 全挂 a2——各恰一卡落 a1 带结果', () => {
    // ===== 第 1 轮：并行声明两个工具 =====
    h.handleStreamStart(makeEvent('stream_start', {}))
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, id: 'call_P', name: 't_pack', arguments_delta: '{"a":1}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'tool_call', id: 'call_P', name: 't_pack', arguments: '{"a":1}' } }))
    h.handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 1, id: 'call_Q', name: 'u_pack', arguments_delta: '{"b":2}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'tool_call', id: 'call_Q', name: 'u_pack', arguments: '{"b":2}' } }))
    h.handleNewMessage(makeEvent('new_message', {
      sequence: 3,
      message: makeServerMessage(A1, 3, [
        { id: 'call_P', name: 't_pack', args: '{"a":1}' },
        { id: 'call_Q', name: 'u_pack', args: '{"b":2}' },
      ]),
    }))
    h.handleStreamEnd(makeEvent('stream_end', {
      final_sequence: 3,
      full_content: '',
      parts: [
        { type: 'tool_call', callId: 'call_P', name: 't_pack', args: { a: 1 }, state: 'calling', sequence: 0 },
        { type: 'tool_call', callId: 'call_Q', name: 'u_pack', args: { b: 2 }, state: 'calling', sequence: 1 },
      ],
    }))
    expect(cardsOf('call_P')).toHaveLength(1)
    expect(cardsOf('call_Q')).toHaveLength(1)

    // ===== 第 2 轮：执行事件全挂 a2 =====
    h.handleStreamStart(makeEvent('stream_start', { message_id: A2 }))
    h.handleToolStart(makeEvent('tool_start', { message_id: A2, call_id: 'call_P', tool_name: 't_pack', args: { a: 1 } }))
    h.handleToolStart(makeEvent('tool_start', { message_id: A2, call_id: 'call_Q', tool_name: 'u_pack', args: { b: 2 } }))
    h.handleToolResult(makeEvent('tool_result', { message_id: A2, call_id: 'call_P', tool_name: 't_pack', result: 'P完成', success: true, duration_ms: 100 }))
    h.handleToolResult(makeEvent('tool_result', { message_id: A2, call_id: 'call_Q', tool_name: 'u_pack', result: 'Q完成', success: true, duration_ms: 200 }))
    h.handleNewMessage(makeEvent('new_message', {
      message_id: A2,
      sequence: 4,
      message: makeServerMessage(A2, 4, [{ id: 'call_Y', name: 'next_tool', args: '{"x":1}' }]),
    }))

    expect(cardsOf('call_P')).toHaveLength(1)
    expect(cardsOf('call_P')[0]).toMatchObject({ msgId: A1, state: 'done', result: 'P完成', durationMs: 100 })
    expect(cardsOf('call_Q')).toHaveLength(1)
    expect(cardsOf('call_Q')[0]).toMatchObject({ msgId: A1, state: 'done', result: 'Q完成', durationMs: 200 })
    // a2 只有自己的 call_Y，无 call_P/call_Q 野卡
    const a2Cards = allToolCards().filter((p) => p.msgId === A2)
    expect(a2Cards).toHaveLength(1)
    expect(a2Cards[0].callId).toBe('call_Y')
  })

  it('场景3 真丢失兜底不回退：全管道无任何声明卡时 tool_result 仍走 FIXUP 建卡', () => {
    h.handleStreamStart(makeEvent('stream_start', {}))
    // tool_start 断线丢失，直接收到 result：无声明卡可归并 → 既有自愈语义保持
    h.handleToolResult(makeEvent('tool_result', {
      call_id: 'call_LOST', tool_name: 'lost_tool', result: 'r', success: true, duration_ms: 5,
    }))
    const cards = cardsOf('call_LOST')
    expect(cards).toHaveLength(1)
    expect(cards[0]).toMatchObject({ msgId: A1, state: 'done', result: 'r', durationMs: 5 })
  })

  it('场景4 终态不回退：call_X 已 done 后迟到的 tool_start（挂任意 message id）不得打回 calling', () => {
    h.handleStreamStart(makeEvent('stream_start', {}))
    // 直接寻址命中路径建卡 + 终态
    h.handleToolStart(makeEvent('tool_start', { call_id: 'call_X', tool_name: 'hello_pack', args: { name: '世界' } }))
    h.handleToolResult(makeEvent('tool_result', { call_id: 'call_X', tool_name: 'hello_pack', result: 'ok', success: true }))
    expect(cardsOf('call_X')[0].state).toBe('done')

    // 迟到的 tool_start 挂在另一条消息 id 上（轮次错位/重放形态）
    h.handleStreamStart(makeEvent('stream_start', { message_id: A2 }))
    h.handleToolStart(makeEvent('tool_start', { message_id: A2, call_id: 'call_X', tool_name: 'hello_pack', args: { name: '世界' } }))

    expect(cardsOf('call_X')).toHaveLength(1)
    expect(cardsOf('call_X')[0]).toMatchObject({ msgId: A1, state: 'done', result: 'ok' })
    // 迟到事件不得在 a2 留下任何卡
    expect(allToolCards().filter((p) => p.msgId === A2)).toHaveLength(0)
  })
})
