/** @feature FP-0.2.可观测性 WS 工具结果写入面截断 @ci frontend-test */
/** @ci frontend-test */
/**
 * handleToolResult 写入面截断测试（数据面瘦身批次 1.2）：
 * 结果体超阈值 → part 只存「预览+标记」（与 session.ts 冷路径收敛同一纯函数）；
 * 阈值内原样；sequence 取消息当前权威值，流式占位未挂 seq 时标记不带 sequence。
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-tool-truncate-001'
const MESSAGE_ID = 'msg_tool_truncate_01'
const THREAD_ID = 'thread-tool-truncate-001'

function makeEvent(type: string, data: Record<string, any>) {
  return {
    type,
    data: {
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      ...data,
    },
    source_type: 'system',
    source_id: PIPELINE_ID,
    timestamp: new Date().toISOString(),
  }
}

describe('handleToolResult 写入面截断', () => {
  let store: any
  let handleToolStart: any
  let handleToolResult: any

  beforeEach(async () => {
    store = await resetStreamingStore(PIPELINE_ID, THREAD_ID, PIPELINE_ID)
    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleToolStart = handlerMod.handleToolStart
    handleToolResult = handlerMod.handleToolResult
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  function runTool(resultPayload: Record<string, any>, msgSequence?: number): any {
    handleToolStart(makeEvent('tool_start', { call_id: 'call-1', tool_name: 'demo_tool' }))
    if (msgSequence !== undefined) {
      // 模拟 stream_end 对账后消息已挂权威 sequence
      store.setState((s: any) => ({
        messagesByPipeline: {
          ...s.messagesByPipeline,
          [PIPELINE_ID]: s.messagesByPipeline[PIPELINE_ID].map((m: any) =>
            m.id === MESSAGE_ID ? { ...m, sequence: msgSequence } : m,
          ),
        },
      }))
    }
    handleToolResult(makeEvent('tool_result', { call_id: 'call-1', tool_name: 'demo_tool', ...resultPayload }))
    const msgs = store.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
    return (msg?.parts ?? []).find((p: any) => p.type === 'tool_call' && p.callId === 'call-1')
  }

  it('大结果 → 截断标记（preview 2KB / totalBytes / sequence=消息权威 seq），state 终态不受影响', () => {
    const bigData = { blob: 'x'.repeat(9 * 1024) }
    const part = runTool(
      { success: true, result: 'y'.repeat(9 * 1024), result_data: bigData },
      7,
    )
    expect(part.state).toBe('done')
    expect(part.result).toMatchObject({ __truncated: true, totalBytes: 9 * 1024, sequence: 7 })
    expect(part.resultData).toMatchObject({
      __truncated: true,
      totalBytes: JSON.stringify(bigData).length,
      sequence: 7,
    })
    expect(part.result.preview.length).toBe(2 * 1024)
  })

  it('小结果 → 原样写入（无标记）', () => {
    const part = runTool({ success: true, result: '晴 26℃', result_data: { temp: 26 } })
    expect(part.result).toBe('晴 26℃')
    expect(part.resultData).toEqual({ temp: 26 })
  })

  it('占位未挂 sequence → 标记不带 sequence（展开回填如实报不可回取）', () => {
    const part = runTool({ success: true, result: 'z'.repeat(9 * 1024) })
    expect(part.result).toMatchObject({ __truncated: true })
    expect((part.result as any).sequence).toBeUndefined()
  })
})
