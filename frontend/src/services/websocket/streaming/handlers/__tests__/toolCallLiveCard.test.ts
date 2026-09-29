// @feature: FP-T12 前端适配(工具调用实时卡渲染链路) | @ci: frontend-test
/**
 * 工具调用「输出即建卡、一卡贯穿始终」端到端时序测试（ADR 2026-09-28-tool-call-live-card）。
 *
 * 锁定用户报障场景的完整生命周期：LLM 输出工具参数（块协议）即建卡（calling
 * → running 视觉）→ new_message 轮收尾（无终态证据 → pending，不显示假「已完成」）
 * → tool_start 执行开始（归并回 calling + 全量 args）→ tool_result 终态。
 *
 * 贯穿性质断言：全程恰好一张卡（单键 call_id 归并）；tool_result 到达前
 * state 恒非终态 done（执行期间不得显示已完成）。
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

const PIPELINE_ID = 'pipe-tool-live-card-001'
const MESSAGE_ID = 'msg_tool_live_card_01'
const THREAD_ID = 'thread-tool-live-card-001'

const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)

/** 后端 new_message.data.message 完整形态（toolCalls 无结果字段——工具尚未执行） */
function makeServerMessage() {
  return {
    id: MESSAGE_ID,
    role: 'assistant',
    content: '我去查一下',
    sequence: 5,
    reasoningContent: null,
    toolCalls: [
      { id: 'call_live', type: 'function', function: { name: 'search', arguments: '{"q":"天气"}' } },
    ],
    timestamp: '2026-09-28T00:00:00Z',
    status: 'completed',
    thread_id: THREAD_ID,
  }
}

describe('工具调用输出即建卡（live card）', () => {
  let store: any
  let h: Record<string, any>

  beforeEach(async () => {
    await resetStreamingStore(PIPELINE_ID, THREAD_ID)
    store = (window as any).__pipelineStore
    const handlerMod = await import('@/services/websocket/streaming/handlers')
    h = handlerMod
    h.handleStreamStart(makeEvent('stream_start', {}))
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  function toolParts() {
    const msg = store.getState().getMessages(PIPELINE_ID).find((m: any) => m.id === MESSAGE_ID)
    return ((msg?.parts || []).filter((p: any) => p.type === 'tool_call')) as Array<Record<string, any>>
  }

  it('完整交错时序：输出建卡(calling) → new_message(pending 非假 done) → tool_start(calling) → tool_result(done)，全程单卡', () => {
    // 输出阶段：正文流式中途开始输出工具调用参数（真实交错形态）
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 0, text: '我去查一下' }))
    h.handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 1, id: 'call_live', name: 'search', arguments_delta: '{"q":"天气"}' }))

    // 输出阶段即建卡：running 可见，不等工具执行
    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0]).toMatchObject({ callId: 'call_live', name: 'search', state: 'calling' })
    h.handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'tool_call', id: 'call_live', name: 'search', arguments: '{"q":"天气"}' } }))
    expect(toolParts()[0].args).toEqual({ q: '天气' })

    // 轮收尾：new_message 先于工具执行到达（引擎轮模型）——无终态证据不得
    // 显示假「已完成」（本次修复的报障核心）
    h.handleNewMessage(makeEvent('new_message', { sequence: 5, message: makeServerMessage() }))
    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0].state).not.toBe('done')
    expect(toolParts()[0].result).toBeUndefined()

    // 执行阶段：tool_start 归并进同一张卡（全量 args + containerTaskId + calling）
    h.handleToolStart(makeEvent('tool_start', {
      call_id: 'call_live', tool_name: 'search', args: { q: '天气' }, container_task_id: 'ct-9',
    }))
    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0]).toMatchObject({ state: 'calling', containerTaskId: 'ct-9' })

    // 执行中（此刻到 tool_result 之间 = 用户应看到 running 的窗口）
    // 终态：tool_result
    h.handleToolResult(makeEvent('tool_result', {
      call_id: 'call_live', tool_name: 'search', result: '晴 26℃', success: true, duration_ms: 1500.0,
    }))
    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0]).toMatchObject({ state: 'done', result: '晴 26℃', durationMs: 1500.0 })
  })

  it('tool_start 归并既有卡：回填缺位字段不重复建卡；已有终态证据的迟到事件不回退状态', () => {
    // 向量①：块协议建卡（name 已知、args 空）→ tool_start 回填 args/containerTaskId，
    // 保留已知名，单卡
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, id: 'c-1', name: 'fs' }))
    h.handleToolStart(makeEvent('tool_start', { call_id: 'c-1', tool_name: 'fs', args: { path: 'a' }, container_task_id: 'ct-1' }))

    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0]).toMatchObject({
      callId: 'c-1', name: 'fs', args: { path: 'a' }, state: 'calling', containerTaskId: 'ct-1',
    })

    // 向量②：tool_result 先落终态（乱序/重放）→ 迟到的 tool_start 不把 done 回退成 calling
    h.handleToolResult(makeEvent('tool_result', { call_id: 'c-1', tool_name: 'fs', result: 'ok', success: true }))
    h.handleToolStart(makeEvent('tool_start', { call_id: 'c-1', tool_name: 'fs', args: { path: 'a' } }))

    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0].state).toBe('done')
  })
})
