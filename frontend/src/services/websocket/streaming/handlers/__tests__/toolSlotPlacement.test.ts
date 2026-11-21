// @feature: FP-T12 前端适配(工具卡宿主消息槽位) | @ci: frontend-test
/**
 * 工具卡宿主消息的权威槽位（ADR 2026-11-06-round-entry-slot-placeholder
 * 的工具事件延伸）：工具卡容器不能无 seq 垫底。
 *
 * 症状（用户实证）：中断后工具卡片消息无 seq，被 compareMessages 判为
 * Number.MAX_SAFE_INTEGER 恒排末尾，永远渲染在消息流最后。
 *
 * 三条不变量：
 * 1. 工具事件携带宿主消息的权威 seq（引擎轮次入口保留槽位）时，新建容器
 *    直接带 seq；既有容器缺 seq → 补落，已有值绝不覆写。
 * 2. stream_start 后到（工具事件先建容器）→ 占位补齐权威 seq——旧实现
 *    在「同 id 已存在」处提前返回，seq 被丢在地上。
 * 3. 旧内核（事件无 seq）→ 维持挂空回落，不劣化。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { compareMessages } from '@/utils/messageOrder'
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

const PIPELINE_ID = 'pipe-tool-slot-001'
const MESSAGE_ID = 'a_tool_slot_01'
const THREAD_ID = 'thread-tool-slot-001'

const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)

describe('工具卡宿主消息权威槽位（中断后不垫底）', () => {
  let store: any
  let h: Record<string, any>

  beforeEach(async () => {
    await resetStreamingStore(PIPELINE_ID, THREAD_ID)
    store = (window as any).__pipelineStore
    h = await import('@/services/websocket/streaming/handlers')
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  function messageOf(id = MESSAGE_ID) {
    return store.getState().getMessages(PIPELINE_ID).find((m: any) => m.id === id)
  }

  function toolParts(id = MESSAGE_ID) {
    return ((messageOf(id)?.parts || []).filter((p: any) => p.type === 'tool_call')) as Array<
      Record<string, any>
    >
  }

  it('(a) 工具事件先到且携带 seq：新建宿主消息直接获得权威 seq', () => {
    h.handleToolStart(
      makeEvent('tool_start', { sequence: 7, call_id: 'call_a', tool_name: 'bash', args: {} }),
    )
    const msg = messageOf()
    expect(msg).toBeDefined()
    expect(toolParts()).toHaveLength(1)
    expect(msg.sequence).toBe(7)
  })

  it('(b) 既有容器缺 seq + stream_start 后到：补齐权威 seq（旧实现提前返回丢失）', () => {
    // 工具事件先建容器（旧内核形态：事件不带 seq）
    h.handleToolStart(
      makeEvent('tool_start', { call_id: 'call_b', tool_name: 'bash', args: {} }),
    )
    expect(messageOf().sequence).toBeUndefined()

    h.handleStreamStart(makeEvent('stream_start', { sequence: 9 }))
    expect(messageOf().sequence).toBe(9)
    expect(toolParts(), '补 seq 不得动工具卡').toHaveLength(1)
  })

  it('(c) 已有权威 seq 不被后到的不同值覆写（幂等/迟到事件不重排）', () => {
    h.handleStreamStart(makeEvent('stream_start', { sequence: 5 }))
    h.handleToolStart(
      makeEvent('tool_start', { sequence: 11, call_id: 'call_c', tool_name: 'bash', args: {} }),
    )
    expect(messageOf().sequence).toBe(5)
  })

  it('(d) 中断收口：宿主消息保持权威 seq，排序不落到已有序消息之后', () => {
    h.handleToolStart(
      makeEvent('tool_start', { sequence: 7, call_id: 'call_d', tool_name: 'bash', args: {} }),
    )
    // 会话里已有更晚的权威消息（下一轮/其他管道消息）
    store.getState().addMessage(PIPELINE_ID, {
      id: 'a_after_stop',
      sessionId: THREAD_ID,
      role: 'assistant',
      content: '后一条',
      timestamp: new Date(Date.now() + 1000).toISOString(),
      parentId: null,
      status: 'completed',
      sequence: 8,
    })

    // 用户点停：本地终态化（内核取消链路收尾前）
    store.getState().stopStreaming(PIPELINE_ID)
    const stopped = messageOf()
    expect(stopped.status).toBe('interrupted')
    expect(stopped.sequence).toBe(7)

    const after = messageOf('a_after_stop')
    expect(compareMessages(stopped, after), '中断消息必须排在后续消息之前，不得恒垫底').toBeLessThan(0)
  })

  it('(e) tool_result 先到且宿主缺失：按事件 seq 补建宿主并落终态卡（结果不丢）', () => {
    h.handleToolResult(
      makeEvent('tool_result', {
        sequence: 12,
        call_id: 'call_e',
        tool_name: 'bash',
        success: true,
        result: 'ok',
      }),
    )
    const msg = messageOf()
    expect(msg, '结果到达必须有落点').toBeDefined()
    expect(msg.sequence).toBe(12)
    expect(toolParts()).toHaveLength(1)
    expect(toolParts()[0].state).toBe('done')
  })

  it('(f) 旧内核（事件无 seq）：维持挂空回落，不劣化', () => {
    h.handleToolStart(
      makeEvent('tool_start', { call_id: 'call_f', tool_name: 'bash', args: {} }),
    )
    expect(messageOf().sequence).toBeUndefined()
  })

  it('(g) 信封顶层 sequence（事件总线计数器）不得当消息序消费', () => {
    h.handleToolStart({
      ...makeEvent('tool_start', { call_id: 'call_g', tool_name: 'bash', args: {} }),
      sequence: 99999,
    })
    expect(messageOf().sequence).toBeUndefined()
  })
})
