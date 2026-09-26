// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 流式 handler 缺口补测（toolHandler / blockHandler / utils）
 *
 * 断行为：事件 → 消息 parts 的可观察组装结果、store 写入/清理副作用、
 * 防御性丢弃（不产生任何 part / 不改状态）。
 *
 * 覆盖契约：
 * - tool_start：缺 call_id / 缺 tool_name → 跳过且不建 part（两组有区分度缺失输入）；
 * - tool_start：消息占位不存在 → 自动补建占位再建卡（FIXUP 自愈）；
 * - tool_result：part 不存在 → 补建 tool_call 卡再写结果；
 * - tool_result：name 仍为 'unknown' 而事件带有效 tool_name → 回填 name；
 * - tool_result：pipeline_id 缺失 → 跳过；
 * - block_start：index 非法 / block_type 未知 → 跳过（不建 part）；
 * - block_start：tool_call 块二次开启 → 累积态幂等（不重复建累积）；
 * - tool_call_delta：增量无消费面，但仍登记块状态（后续 delta 不抛错）；
 * - mergeStreamingParts：无 server parts 且本地有 streaming 态 → 收敛为 done；
 * - mergeStreamingParts：默认模式本地无实质内容 → 用 server（含 server 文本更长时校准）；
 * - mergeStreamingParts：tool_call 补充携带 result → 回填基底；
 * - ensureStreamingPlaceholder：占位已存在（同 id）→ 原地保留不清空内容；
 * - ensureStreamingPlaceholder：上一轮 streaming 残留（无文本/无未解析卡片）→ 清理。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-handler-gaps-001'
const MESSAGE_ID = 'msg_handler_gaps_01'
const THREAD_ID = 'thread-handler-gaps-001'

const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)

describe('流式 handler 缺口补测', () => {
  let store: any
  let h: Record<string, any>

  beforeEach(async () => {
    vi.useFakeTimers()
    store = await resetStreamingStore(PIPELINE_ID, THREAD_ID)
    h = await import('@/services/websocket/streaming/handlers')
    h.handleStreamStart(makeEvent('stream_start', {}))
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  function parts(): any[] {
    const msgs = store.getState().getMessages(PIPELINE_ID)
    return msgs.find((m: any) => m.id === MESSAGE_ID)?.parts ?? []
  }

  describe('tool_start 防御与自愈', () => {
    it('缺 call_id → 跳过（不建卡）；补上 call_id 后同一事件建卡成功', () => {
      h.handleToolStart(makeEvent('tool_start', { tool_name: 'search' }))
      expect(parts().filter((p: any) => p.type === 'tool_call')).toHaveLength(0)

      h.handleToolStart(makeEvent('tool_start', { call_id: 'c-1', tool_name: 'search' }))
      const cards = parts().filter((p: any) => p.type === 'tool_call')
      expect(cards).toHaveLength(1)
      expect(cards[0].callId).toBe('c-1')
      expect(cards[0].name).toBe('search')
    })

    it('缺 tool_name → 跳过（不建卡，等数据完整）', () => {
      h.handleToolStart(makeEvent('tool_start', { call_id: 'c-2' }))
      expect(parts().filter((p: any) => p.type === 'tool_call')).toHaveLength(0)
    })

    it('消息占位不存在 → 自动补建占位消息后建卡（断线/乱序自愈）', async () => {
      // 清空消息面：模拟 stream_start 丢失，tool_start 先到
      store.setState({ messagesByPipeline: {} })

      h.handleToolStart(makeEvent('tool_start', { call_id: 'c-3', tool_name: 'grep' }))

      const msgs = store.getState().getMessages(PIPELINE_ID)
      const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
      expect(msg).toBeTruthy()
      expect(msg.parts.filter((p: any) => p.type === 'tool_call')).toHaveLength(1)
    })

    it('缺 pipeline_id → 整体跳过（不建卡也不建占位）', () => {
      store.setState({ messagesByPipeline: {} })
      h.handleToolStart({ type: 'tool_start', data: { message_id: MESSAGE_ID, call_id: 'c-4', tool_name: 'x' } })
      expect(store.getState().getMessages(PIPELINE_ID)).toHaveLength(0)
    })
  })

  describe('tool_result 自愈与 name 回填', () => {
    it('part 不存在 → 补建 tool_call 卡并写入结果（tool_start 丢失兜底）', () => {
      h.handleToolResult(
        makeEvent('tool_result', { call_id: 'c-miss', tool_name: 'read_file', success: true, result: '内容' }),
      )

      const cards = parts().filter((p: any) => p.type === 'tool_call')
      expect(cards).toHaveLength(1)
      expect(cards[0].callId).toBe('c-miss')
      expect(cards[0].name).toBe('read_file')
      expect(cards[0].state).toBe('done')
      expect(cards[0].result).toBe('内容')
    })

    it('name 仍为 unknown 且 result 携带有效 tool_name → 回填 name', () => {
      // 先以 unknown 建卡（tool_start 缺 tool_name 时不会建卡，故直接用 result 的 FIXUP 路径建）
      h.handleToolResult(makeEvent('tool_result', { call_id: 'c-n1', success: true, result: 'r' }))
      expect(parts().find((p: any) => p.callId === 'c-n1')?.name).toBe('unknown')

      // 同 callId 的第二次 result 带 tool_name → 回填
      h.handleToolResult(
        makeEvent('tool_result', { call_id: 'c-n1', tool_name: 'shell_exec', success: true, result: 'r2' }),
      )
      const card = parts().find((p: any) => p.callId === 'c-n1')
      expect(card.name).toBe('shell_exec')
      expect(card.result).toBe('r2')
    })

    it('已具名卡片的 result 不回写为事件名（权威名不被覆盖）', () => {
      h.handleToolStart(makeEvent('tool_start', { call_id: 'c-n2', tool_name: 'first_name' }))
      h.handleToolResult(
        makeEvent('tool_result', { call_id: 'c-n2', tool_name: 'second_name', success: true, result: 'ok' }),
      )
      const card = parts().find((p: any) => p.callId === 'c-n2')
      expect(card.name).toBe('first_name')
      expect(card.state).toBe('done')
    })

    it('缺 pipeline_id → 跳过（不产生任何 part）', () => {
      store.setState({ messagesByPipeline: {} })
      h.handleToolResult({ type: 'tool_result', data: { message_id: MESSAGE_ID, call_id: 'c-5', tool_name: 'x' } })
      expect(store.getState().getMessages(PIPELINE_ID)).toHaveLength(0)
    })
  })

  describe('block_start / tool_call_delta 防御', () => {
    it('index 非法（负数 / 非整数 / 缺失）→ 跳过，不建任何 part', () => {
      h.handleBlockStart(makeEvent('block_start', { index: -1, block_type: 'text' }))
      h.handleBlockStart(makeEvent('block_start', { index: 1.5, block_type: 'text' }))
      h.handleBlockStart(makeEvent('block_start', { block_type: 'text' }))
      expect(parts()).toHaveLength(0)
    })

    it('block_type 未知 → 跳过；随后合法 text 块正常建 part', () => {
      h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'image' }))
      expect(parts()).toHaveLength(0)

      h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
      h.handleTextDelta(makeEvent('text_delta', { index: 0, text: '正文' }))
      h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'text' } }))
      expect(parts()).toHaveLength(1)
      expect(parts()[0]).toMatchObject({ type: 'text', content: '正文', state: 'done' })
    })

    it('tool_call 块重复开启（同 index）→ 幂等：不产生工具卡（卡面由契约事件建）', () => {
      h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
      h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
      expect(parts().filter((p: any) => p.type === 'tool_call')).toHaveLength(0)
    })

    it('tool_call_delta 无消费面但仍登记块状态：后续同名 delta 不抛错且不建卡', () => {
      h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
      h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, arguments_delta: '{"a"' }))
      h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, arguments_delta: ':1}' }))
      expect(parts().filter((p: any) => p.type === 'tool_call')).toHaveLength(0)
    })

    it('tool_call_delta 未经 block_start（块状态缺失）→ 自建累积态且不抛错', () => {
      expect(() =>
        h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, arguments_delta: '{}' })),
      ).not.toThrow()
      expect(parts().filter((p: any) => p.type === 'tool_call')).toHaveLength(0)
    })

    it('delta 先于 block_start（乱序）→ 复用同类型 streaming part，不重复建 part', () => {
      h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
      h.handleTextDelta(makeEvent('text_delta', { index: 0, text: 'A' }))

      // 新块索引的 delta 先到（block_start 丢失）：复用仍在 streaming 的 text part
      h.handleTextDelta(makeEvent('text_delta', { index: 9, text: 'B' }))
      vi.advanceTimersByTime(50)

      const textParts = parts().filter((p: any) => p.type === 'text')
      expect(textParts).toHaveLength(1)
      expect(textParts[0].content).toBe('AB')
    })
  })
})

describe('utils: mergeStreamingParts 与占位管理', () => {
  let store: any
  let utils: Record<string, any>

  beforeEach(async () => {
    vi.useFakeTimers()
    store = await resetStreamingStore(PIPELINE_ID, THREAD_ID)
    utils = await import('@/services/websocket/streaming/handlers/utils')
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  it('无 server parts：本地流式态收敛为 done；无残留态时保持原引用（两组区分输入）', () => {
    const streaming = [{ type: 'text', content: '半截', state: 'streaming' }]
    const settled = [{ type: 'text', content: '完整', state: 'done' }]

    const a = utils.mergeStreamingParts(streaming, undefined, undefined, '半截')
    expect(a.parts[0].state).toBe('done')
    expect(a.content).toBe('半截')

    const b = utils.mergeStreamingParts(settled, undefined, undefined, '完整')
    expect(b.parts).toBe(settled)

    // serverFullContent 更长 → 采用 server 文本
    const c = utils.mergeStreamingParts(settled, undefined, '更长的权威文本', '完整')
    expect(c.content).toBe('更长的权威文本')
  })

  it('本地空占位（无实质内容）→ 直接用 server parts，本地残留被替换', () => {
    const local = [{ type: 'text', content: '', state: 'streaming' }]
    const server = [{ type: 'text', content: '服务端权威', state: 'done', sequence: 1 }]

    const { parts, content } = utils.mergeStreamingParts(local, server, '服务端权威', '')

    expect(parts).toBe(server)
    expect(content).toBe('服务端权威')
  })

  it('tool_call 结果增量回填基底（不覆盖基底已有权威字段）', () => {
    const base = [
      { type: 'tool_call', callId: 't1', name: 'search', result: undefined, state: 'done' },
      { type: 'tool_call', callId: 't2', name: 'search', result: '基底已有', state: 'done' },
    ]
    const supplement = [
      { type: 'tool_call', callId: 't1', name: 'search', result: '补入结果', durationMs: 1200 },
      { type: 'tool_call', callId: 't2', name: 'search', result: '被忽略的覆盖尝试' },
    ]

    const { parts } = utils.mergeStreamingParts(base, supplement, '', '')

    expect(parts.find((p: any) => p.callId === 't1')?.result).toBe('补入结果')
    expect(parts.find((p: any) => p.callId === 't1')?.durationMs).toBe(1200)
    expect(parts.find((p: any) => p.callId === 't2')?.result).toBe('基底已有')
  })

  it('supplement 携带基底未有的 tool_call（快照截断）→ 追加保留', () => {
    const base = [{ type: 'text', content: '正文', state: 'done' }]
    const supplement = [
      { type: 'text', content: '正文', state: 'done' },
      { type: 'tool_call', callId: 'extra-1', name: 'late_tool', state: 'done' },
    ]

    const { parts } = utils.mergeStreamingParts(base, supplement, '', '')

    expect(parts.map((p: any) => p.type)).toEqual(['text', 'tool_call'])
    expect(parts[1].callId).toBe('extra-1')
  })

  it('ensureStreamingPlaceholder：同 id 已存在 → 原地保留（内容不被清空）', () => {
    utils.ensureStreamingPlaceholder(PIPELINE_ID, MESSAGE_ID, THREAD_ID)
    store.getState().appendPart(PIPELINE_ID, MESSAGE_ID, { type: 'text', content: '已有内容', state: 'streaming' })

    utils.ensureStreamingPlaceholder(PIPELINE_ID, MESSAGE_ID, THREAD_ID)

    const msg = store.getState().getMessages(PIPELINE_ID).find((m: any) => m.id === MESSAGE_ID)
    expect(msg.parts).toHaveLength(1)
    expect(msg.parts[0].content).toBe('已有内容')
    expect(store.getState().getMessages(PIPELINE_ID)).toHaveLength(1)
  })

  it('ensureStreamingPlaceholder：上一轮无内容 streaming 残留 → 本轮清理（只留新占位）', () => {
    // 上一轮残留：有文本但流未收尾（streaming 且有内容）→ 收尾为 completed 保留
    store.getState().addMessage(PIPELINE_ID, {
      id: 'old-msg-with-text',
      sessionId: THREAD_ID,
      role: 'assistant',
      content: '上一轮内容',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: 1,
      status: 'streaming',
    } as any)
    // 另一条残留：完全无内容 → 移除
    store.getState().addMessage(PIPELINE_ID, {
      id: 'old-msg-empty',
      sessionId: THREAD_ID,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: 2,
      status: 'streaming',
    } as any)

    utils.ensureStreamingPlaceholder(PIPELINE_ID, MESSAGE_ID, THREAD_ID)

    const msgs = store.getState().getMessages(PIPELINE_ID)
    const ids = msgs.map((m: any) => m.id)
    expect(ids).toContain('old-msg-with-text')
    expect(ids).not.toContain('old-msg-empty')
    expect(ids).toContain(MESSAGE_ID)
    expect(msgs.find((m: any) => m.id === 'old-msg-with-text')?.status).toBe('completed')
  })

  it('ensureStreamingPlaceholder：上一轮残留带未解析 tool_call（calling）→ 移除（后端对账补回）', () => {
    store.getState().addMessage(PIPELINE_ID, {
      id: 'old-msg-pending-tool',
      sessionId: THREAD_ID,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: 1,
      status: 'streaming',
    } as any)
    store.getState().appendPart(PIPELINE_ID, 'old-msg-pending-tool', {
      type: 'tool_call',
      callId: 'stale-call',
      name: 'search',
      args: {},
      state: 'calling',
    })

    utils.ensureStreamingPlaceholder(PIPELINE_ID, MESSAGE_ID, THREAD_ID)

    expect(store.getState().getMessages(PIPELINE_ID).map((m: any) => m.id)).not.toContain(
      'old-msg-pending-tool',
    )
  })

  it('ensureStreamingPlaceholder：上一轮残留的非文本 part（system）原样保留，只收敛 text/thinking 与工具卡', () => {
    store.getState().addMessage(PIPELINE_ID, {
      id: 'old-msg-system-part',
      sessionId: THREAD_ID,
      role: 'assistant',
      content: '有内容',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: 1,
      status: 'streaming',
    } as any)
    store.getState().appendPart(PIPELINE_ID, 'old-msg-system-part', {
      type: 'system',
      content: '系统提示',
    } as any)

    utils.ensureStreamingPlaceholder(PIPELINE_ID, MESSAGE_ID, THREAD_ID)

    const kept = store
      .getState()
      .getMessages(PIPELINE_ID)
      .find((m: any) => m.id === 'old-msg-system-part')
    expect(kept.status).toBe('completed')
    // 非 text/thinking/tool_call part 原样保留（不误改类型）
    expect(kept.parts[0]).toMatchObject({ type: 'system', content: '系统提示' })
  })

  it('ensureStreamingPlaceholder：backendSequence 透传（正数采用、0/未传挂空）', () => {
    utils.ensureStreamingPlaceholder(PIPELINE_ID, 'msg-with-seq', THREAD_ID, 42)
    expect(
      store.getState().getMessages(PIPELINE_ID).find((m: any) => m.id === 'msg-with-seq')?.sequence,
    ).toBe(42)

    utils.ensureStreamingPlaceholder(PIPELINE_ID, 'msg-no-seq', THREAD_ID, 0)
    expect(
      store.getState().getMessages(PIPELINE_ID).find((m: any) => m.id === 'msg-no-seq')?.sequence,
    ).toBeUndefined()
  })

  it('extractMessageId：顶层 / data.message_id / data.ai_message_id 三形态与空值', () => {
    expect(utils.extractMessageId({ message_id: 'top' })).toBe('top')
    expect(utils.extractMessageId({ data: { message_id: 'inner' } })).toBe('inner')
    expect(utils.extractMessageId({ data: { ai_message_id: 'ai' } })).toBe('ai')
    expect(utils.extractMessageId(null)).toBeNull()
  })
})
