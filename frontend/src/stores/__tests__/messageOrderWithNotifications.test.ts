/**
 * 系统通知 + 注入消息 + 刷新复杂场景的消息顺序测试。
 *
 * 【initFromAPI 新语义】initFromAPI 现为全量替换：完全丢弃本地所有消息，仅使用传入的 API 数据。
 * 不再 merge、不再保护 localOnly（不保护 streaming、不保护 grace、不保留 system 通知、不保留 optimistic user）。
 * 刷新 = 从后端持久化全量重载。后端正在输出时，WS 重连的 backfill 增量补漏 + 续流会补回新内容。
 *
 * 因此本文件分两类断言：
 * 1. 流式期间（未触发 initFromAPI）的到达顺序断言 —— 仍然有效：渲染顺序 = store 数组顺序 = 到达顺序。
 * 2. 触发 initFromAPI 之后的断言 —— 必须 assert「store 恰好等于 API 返回数据，无任何 localOnly 残留」。
 *
 * 设计原则（与 multiturnOrderE2E 对齐）：
 * - 用真实 pipelineMessageStore + 真实 handlers（不 mock store）
 * - 流式期间：渲染顺序 = 到达顺序（system 通知按到达序夹在 AI 气泡之间）
 * - initFromAPI 后：store 仅含 API 权威消息，本地流式/占位/system/optimistic 一律丢弃
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Message } from '@/types/models'

// ── mock 外部依赖（与 multiturnOrderE2E / fix_duplicate_ai_repro 对齐）──
vi.mock('@/utils/activityConverter', () => ({
  toolCallToActivity: (toolCall: any) => ({
    type: 'tool_call',
    id: toolCall.callId ?? toolCall.call_id,
    title: toolCall.name ?? toolCall.tool_name,
    toolName: toolCall.name ?? toolCall.tool_name,
    status: toolCall.state ?? toolCall.status ?? 'pending',
    details: [],
    actions: [],
  }),
}))
vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (base: any) => base,
  getToolCardConfig: () => null,
}))
vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))
vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))
vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = 'pid_order_a000000000'
const THREAD_ID = 'tid_order_b000000000'

let pipelineStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
let handlers: typeof import('@/services/websocket/streaming/handlers')
let handleSystemNotification: typeof import('@/services/websocket/streaming/lifecycleHandlers').handleSystemNotification
let flushStreamChunkBuffer: typeof import('@/services/websocket/streaming/handlers/streamHandler').flushStreamChunkBuffer

/** 构造一条最小可用消息 */
function makeMsg(id: string, overrides: Partial<Message>): Message {
  return {
    id,
    sessionId: THREAD_ID,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: 0,
    status: 'completed',
    ...overrides,
  } as Message
}

/** 构造一个流式 chunk / 工具等 WS 事件（顶层 + data 双层，匹配真实后端 _make_event） */
function evt(type: string, data: Record<string, any>): any {
  return { type, sequence: data.sequence ?? 0, data: { pipeline_id: PIPELINE_ID, ...data } }
}

/** 构造一个 system_notification 事件（resolvePipelineId 取 data.pipeline_id）。
 * 模拟后端 emit_notification：生成 record_id（hex12，唯一 id 来源），
 * 前端用它作消息 id，与 track 落库 record_id 一致。 */
function notificationEvent(content: string, overrides: Record<string, any> = {}): any {
  const recordId = Math.random().toString(16).slice(2, 14).padEnd(12, '0')
  return {
    data: {
      pipeline_id: PIPELINE_ID,
      content,
      level: 'info',
      notification_id: `sys_${Math.random().toString(36).slice(2, 10)}`,
      record_id: recordId,
      ...overrides,
    },
  }
}

/** 刷写 streamChunk 的 RAF 缓冲（jsdom 不自动跑 RAF，需手动调） */
function flush(): void {
  flushStreamChunkBuffer()
}

/** 读取 store 中消息的 id 序列 */
function ids(): string[] {
  return pipelineStore.getState().getMessages(PIPELINE_ID).map((m) => m.id)
}

/** 读取 store 中所有 system 消息的 id 序列（system 消息 id 现为后端 record_id，无固定前缀） */
function systemIds(): string[] {
  return pipelineStore.getState().getMessages(PIPELINE_ID)
    .filter((m) => m.role === 'system')
    .map((m) => m.id)
}

beforeEach(async () => {
  vi.resetModules()
  const storeMod = await import('@/stores/pipelineMessageStore')
  pipelineStore = storeMod.usePipelineMessageStore
  pipelineStore.setState({
    messagesByPipeline: {},
    pipelines: {},
    pipelineSessionMap: {},
    streamingState: {},
    activePipelineId: null,
    topCursorsByPipeline: {},
    bottomCursorsByPipeline: {},
    hasMoreOlderByPipeline: {},
    isLoadingOlderByPipeline: {},
  })
  const h = await import('@/services/websocket/streaming/handlers')
  handlers = h
  const lh = await import('@/services/websocket/streaming/lifecycleHandlers')
  handleSystemNotification = lh.handleSystemNotification
  const sf = await import('@/services/websocket/streaming/handlers/streamHandler')
  flushStreamChunkBuffer = sf.flushStreamChunkBuffer

  pipelineStore.getState().registerPipeline({ pipelineId: PIPELINE_ID, sessionId: THREAD_ID } as any)
  pipelineStore.getState().activatePipeline(PIPELINE_ID)
})

describe('系统通知 + 注入消息 + 刷新的消息顺序', () => {
  it('场景1: 流式占位在 initFromAPI 后被丢弃，store 恰好等于 API 数据', () => {
    const MSG = 'msg_streaming_a000000'

    // 历史消息（API 风格）：user1(seq=1) → ai1(seq=2)
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2, parts: [{ type: 'text', content: '答1', sequence: 1 } as any] }),
    ])
    // 当前 store: [user-1, ai-1]

    // 流式开始：stream_start 不带 sequence → 占位进入本地态
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG, _threadId: THREAD_ID }))
    expect(ids()).toContain(MSG) // 流式期间占位存在

    // 此时刷新（initFromAPI，全量替换语义）
    // 后端真实时序：ai1 之后注入了一条「上级」user 消息（seq=3），流式占位后端还没落库
    // → API 返回 user-injected(seq=3)，占位不在 API 列表里
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2, parts: [{ type: 'text', content: '答1', sequence: 1 } as any] }),
      makeMsg('user-injected', { role: 'user', content: '[上级]继续', sequence: 3 }),
    ])

    // 新语义：API 三条按 sequence 排序，占位被丢弃（不在 API 列表里）
    expect(ids()).toEqual(['user-1', 'ai-1', 'user-injected'])
  })

  it('场景2: 系统通知 + 流式占位 + 切 Tab 刷新，刷新后仅剩 API 数据（system + 占位都丢弃）', () => {
    const MSG = 'msg_streaming_b000000'

    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // 流式占位先到
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG, _threadId: THREAD_ID }))
    // 系统通知后到
    handleSystemNotification(notificationEvent('任务完成'))

    // 流式期间 store 渲染顺序 = 到达顺序：user-1 → 占位 → 通知（在最下面）
    const beforeIds = ids()
    expect(beforeIds[0]).toBe('user-1')
    expect(beforeIds[1]).toBe(MSG)
    expect(systemIds(), '流式期间应有一条 system 通知').toHaveLength(1)
    const sysId = systemIds()[0]
    expect(beforeIds[beforeIds.length - 1]).toBe(sysId)

    // 切 Tab 触发 initFromAPI（全量替换语义）：API 只返回 user-1
    // 新语义：system 通知 / 流式占位都不在 API 列表里 → 全部丢弃，store 恰好等于 API。
    // 后端正在输出时，WS 重连的 backfill 补漏 + 续流会补回 system 通知与流式内容。
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // 期望：store 仅含 API 返回的 user-1，占位与 system 通知都被丢弃
    expect(ids()).toEqual(['user-1'])
    expect(systemIds(), '刷新后 system 通知应被丢弃（API 未返回）').toHaveLength(0)
  })

  it('场景3: 上级注入 user 消息占 sequence + 流式占位，刷新后仅 API 数据保留', () => {
    const MSG = 'msg_streaming_c000000'

    // store 已有 user-1(seq=1) ai-1(seq=2)
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // 用户发送 user-2（乐观，前端 sequence=localMax+1=3）
    pipelineStore.getState().addMessage(PIPELINE_ID, makeMsg('user-2', {
      role: 'user', content: '问2', sequence: 3, clientMessageId: 'user-2',
    }))
    // 流式占位（前端 sequence=localMax+1=4）
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG, _threadId: THREAD_ID }))
    expect(ids()).toContain(MSG) // 流式期间占位存在

    // 后端真实时序：后端把 user-2 落库 seq=3，但中间插入了上级 user-injected(后端 seq=4)，
    // 流式占位后端 seq=5 尚未落库 → API 返回到 seq=4
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
      makeMsg('user-2', { role: 'user', content: '问2', sequence: 3, clientMessageId: 'user-2' }),
      makeMsg('user-injected', { role: 'user', content: '[上级]补充', sequence: 4 }),
    ])

    // 新语义：API 四条按 sequence 排序，流式占位被丢弃（不在 API 列表里）
    expect(ids()).toEqual(['user-1', 'ai-1', 'user-2', 'user-injected'])
  })

  it('场景4: optimistic grace user 消息 + 流式，刷新后 grace 消息与占位都被丢弃', () => {
    const MSG = 'msg_streaming_d000000'

    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // 刚发送的乐观 user-2（30s grace 窗口内，后端可能尚未持久化）
    pipelineStore.getState().addMessage(PIPELINE_ID, makeMsg('user-2', {
      role: 'user', content: '问2', sequence: 3, clientMessageId: 'user-2',
    }))
    // 流式占位
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG, _threadId: THREAD_ID }))
    expect(ids()).toContain('user-2')
    expect(ids()).toContain(MSG)

    // 刷新（全量替换语义）：后端尚未持久化 user-2 和占位 → API 只返回到 ai-1
    // 新语义：grace user 与流式占位都不在 API 列表里 → 全部丢弃
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // store 恰好等于 API：user-1 + ai-1，grace 与占位都消失
    expect(ids()).toEqual(['user-1', 'ai-1'])
  })

  it('场景5: 回归 — persist 残留的 completed 旧消息不复活 refresh_order 旧 bug', () => {
    // 模拟 persist 残留：一条很旧的 completed assistant 消息（timestamp 远超 30s grace）
    const staleTime = new Date(Date.now() - 60_000).toISOString() // 60s 前，超出 grace
    // 直接 addMessage 模拟 persist 恢复的旧消息
    pipelineStore.getState().addMessage(PIPELINE_ID, makeMsg('stale-ai', {
      role: 'assistant', content: '旧残留', sequence: 0, status: 'completed', timestamp: staleTime,
    }))

    // 刷新：API 返回权威消息（不含 stale-ai）
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // stale-ai 不满足 streaming 也不满足 grace → 应被丢弃（return false）
    // 不会复活 fix_20260623_refresh_order 防的「残留污染顺序」bug
    expect(ids()).toEqual(['user-1', 'ai-1'])
  })

  it('场景6: 回归 — 冷启动纯历史（空本地态）initFromAPI 仍按 sequence 排序', () => {
    // 空本地态，直接冷启动
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-3', { role: 'user', content: '问3', sequence: 3 }),
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-2', { role: 'assistant', content: '答2', sequence: 4 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // API 返回顺序被打乱，冷启动应按 sequence 升序排好（localOnly 为空，纯走 dedupedSorted）
    expect(ids()).toEqual(['user-1', 'ai-1', 'user-3', 'ai-2'])
  })

  // ── fix_20260705_notification_after_reply（initFromAPI 全量替换语义下重写）──
  // 流式期间通知按到达顺序排在 AI 之前，这部分断言不变（rendering = arrival order）。
  // 但刷新（initFromAPI）后，由于 system 通知不在 API 返回列表里，新语义会将其丢弃。
  it('场景7: 系统通知流式期间排在 AI 之前；刷新后 system 通知被丢弃（API 未返回）', () => {
    const AI_MSG = 'msg_ai_after_notif'

    // 冷启动已有历史：user-1(seq=1) ai-1(seq=2)
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // 后端时序：consume_pending_notifications 先推送系统通知（seq=11, 权威）
    handleSystemNotification(notificationEvent('[系统通知] 子任务已完成', {
      sequence: 11, // 后端 emit_notification 下发的权威 sequence
    }))

    // 紧接着 AI 流式回复（seq=12, 权威）
    handlers.handleStreamStart(evt('stream_start', {
      message_id: AI_MSG,
      _threadId: THREAD_ID,
      sequence: 12,
    }))
    // 流式期间补一条文字内容并结束，让占位落定为 completed assistant
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_MSG, content: '查报告' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_MSG,
      _threadId: THREAD_ID,
      final_sequence: 12,
      data: { parts: [{ type: 'text', content: '查报告', sequence: 0 }], full_content: '查报告' },
    }))

    // 流式期间渲染顺序 = 到达顺序：通知先到，AI 后到 → 通知在 AI 前
    const duringIds = ids()
    expect(systemIds(), '流式期间系统通知应已创建').toHaveLength(1)
    const sysId = systemIds()[0]
    const sysIdx = duringIds.indexOf(sysId)
    const aiIdx = duringIds.indexOf(AI_MSG)
    expect(aiIdx, 'AI 回复应已落定').toBeGreaterThan(-1)
    // ★ 流式期间断言：通知（先到）应在 AI（后到）之前
    expect(sysIdx, '流式期间：通知(先到) 应排在 AI(后到) 之前').toBeLessThan(aiIdx)

    // 切 Tab 触发 initFromAPI（全量替换语义）：API 只返回落库的 user-1/ai-1 + AI 回复(seq=12)
    // 系统通知未落库 → 新语义下被丢弃，store 恰好等于 API 数据
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
      makeMsg(AI_MSG, { role: 'assistant', content: '查报告', sequence: 12, status: 'completed' }),
    ])

    // ★ 刷新后断言：system 通知被丢弃（API 未返回），store 仅含 API 三条
    expect(ids()).toEqual(['user-1', 'ai-1', AI_MSG])
    expect(systemIds(), '刷新后 system 通知应被丢弃（API 未返回）').toHaveLength(0)
  })

  // ── fix_20260705_notification_after_reply（initFromAPI 全量替换语义下重写）──
  // 通知延迟到达 + localMax 被推高：旧断言依赖 initFromAPI 后 system 仍保留并按 seq 归并。
  // 新语义下 initFromAPI 全量替换，system 通知不在 API 列表 → 被丢弃。
  it('场景8: 通知延迟到达 + localMax 被推高；刷新后 system 通知被丢弃（API 未返回）', () => {
    const AI_MAIN = 'msg_ai_main_reply'

    // 冷启动历史：user-1(seq=1) ai-1(seq=2)
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // AI 主回复流式落定（seq=12，后端权威）
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_MAIN, _threadId: THREAD_ID, sequence: 12 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_MAIN, content: '先看报告' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_MAIN,
      _threadId: THREAD_ID,
      final_sequence: 12,
      data: { parts: [{ type: 'text', content: '先看报告', sequence: 0 }], full_content: '先看报告' },
    }))

    // 模拟"通知事件延迟到达"——后端实际在 AI 主回复之前推送（seq=11, 后端权威）
    handleSystemNotification(notificationEvent('[系统通知] 子任务已完成', {
      sequence: 11,
    }))

    expect(systemIds(), '流式期间系统通知应已创建').toHaveLength(1)

    // 切 Tab 触发 initFromAPI（全量替换语义）：API 返回落库的 user-1/ai-1 + AI 主回复(seq=12)
    // 系统通知 localOnly 不在 API 列表 → 新语义下被丢弃
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
      makeMsg(AI_MAIN, { role: 'assistant', content: '先看报告', sequence: 12, status: 'completed' }),
    ])

    // ★ 刷新后断言：store 恰好等于 API 三条，system 通知被丢弃
    expect(ids()).toEqual(['user-1', 'ai-1', AI_MAIN])
    expect(systemIds(), '刷新后 system 通知应被丢弃（API 未返回）').toHaveLength(0)
  })

  // ── fix_20260705_notification_stuck_at_bottom ──────────────────────────
  // 复现「通知固定在最下面、被排到上一轮 AI 之后」的边界情况。
  //
  // 用户洞察：上一轮 AI_A 还在 streaming 时，系统通知 N 推送进来；
  // AI_A 的 stream_end 到达；然后 AI_B 的 stream_start 到达。
  // 期望顺序：[..., AI_A, N, AI_B]（N 夹在两轮 AI 之间）
  // 实际可能：N 被排到 AI_B 之后，或 AI_B 合并进 AI_A 把 N 挤后。
  it('场景9: 上一轮 AI 还在 streaming 时通知到达，通知应夹在两轮 AI 之间', () => {
    const AI_A = 'msg_ai_a_round1'
    const AI_B = 'msg_ai_b_round2'

    // 冷启动历史
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // AI_A 流式开始（还在 streaming，未结束）
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_A, _threadId: THREAD_ID, sequence: 10 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_A, content: 'AI_A 内容' }))
    flush()
    // ★ 注意：AI_A 还在 streaming，没有发 stream_end

    // ★ 此时系统通知推送进来（AI_A 仍在 streaming）
    handleSystemNotification(notificationEvent('[系统通知] 子任务已完成', {
      sequence: 11,
    }))
    expect(systemIds(), '通知应已创建').toHaveLength(1)
    const sysId = systemIds()[0]

    // AI_A 流式结束
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_A,
      _threadId: THREAD_ID,
      final_sequence: 10,
      data: { parts: [{ type: 'text', content: 'AI_A 内容', sequence: 0 }], full_content: 'AI_A 内容' },
    }))

    // AI_B 新一轮流式开始
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_B, _threadId: THREAD_ID, sequence: 12 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_B, content: 'AI_B 内容' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_B,
      _threadId: THREAD_ID,
      final_sequence: 12,
      data: { parts: [{ type: 'text', content: 'AI_B 内容', sequence: 0 }], full_content: 'AI_B 内容' },
    }))

    const finalIds = ids()
    // ★ 期望：N 夹在 AI_A 和 AI_B 之间
    const aIdx = finalIds.indexOf(AI_A)
    const nIdx = finalIds.indexOf(sysId!)
    const bIdx = finalIds.indexOf(AI_B)
    expect(aIdx, 'AI_A 应存在').toBeGreaterThan(-1)
    expect(nIdx, '通知应存在').toBeGreaterThan(-1)
    expect(bIdx, 'AI_B 应存在').toBeGreaterThan(-1)
    expect(
      nIdx,
      `通知应夹在 AI_A 和 AI_B 之间。实际顺序: ${JSON.stringify(finalIds)}`,
    ).toBeGreaterThan(aIdx)
    expect(
      nIdx,
      `通知应在 AI_B 之前（夹在中间）。实际顺序: ${JSON.stringify(finalIds)}`,
    ).toBeLessThan(bIdx)
  })

  // ── fix_20260705_notification_pushed_after_despite_arriving_first ──────
  // 用户精确描述的现场：通知【先到】，AI 消息【后到】，但最终 UI 显示通知在 AI 之后。
  // 怀疑点：ensureStreamingPlaceholder 的合并分支把后到的 AI 内容合并到了通知之前的
  // 旧 streaming AI 消息里，导致通知被"挤"到末尾。
  //
  // 时序：
  //   1. AI_old 还在 streaming（stream_end 未到）
  //   2. 通知 N 推送（先到）→ push 末尾 [AI_old, N]
  //   3. AI_new stream_start 到达（后到）→ ensureStreamingPlaceholder
  //      prevMsg = after[last] = N (system) → 不满足合并 → 应新建
  //   4. 期望 [AI_old, N, AI_new]，N 在中间
  it('场景10: 通知先到 + AI 后到时，通知不应被后到的 AI 挤到末尾', () => {
    const AI_OLD = 'msg_ai_old_streaming'
    const AI_NEW = 'msg_ai_new_after_notif'

    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // 1. AI_old 流式开始（未结束）
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_OLD, _threadId: THREAD_ID, sequence: 10 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_OLD, content: 'AI_old 部分内容' }))
    flush()

    // 2. ★ 通知先到（AI_old 仍在 streaming）
    handleSystemNotification(notificationEvent('[系统通知] 子任务完成', { sequence: 11 }))
    expect(systemIds(), '通知应已创建').toHaveLength(1)
    const sysId = systemIds()[0]

    // 此时数组应为 [user-1, AI_old(streaming), sysId]
    const midIds = ids()
    expect(midIds).toEqual(['user-1', AI_OLD, sysId])

    // 3. ★ AI_new 的 stream_start 后到
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_NEW, _threadId: THREAD_ID, sequence: 12 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_NEW, content: 'AI_new 内容' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_NEW,
      _threadId: THREAD_ID,
      final_sequence: 12,
      data: { parts: [{ type: 'text', content: 'AI_new 内容', sequence: 0 }], full_content: 'AI_new 内容' },
    }))
    // AI_old 也结束
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_OLD,
      _threadId: THREAD_ID,
      final_sequence: 10,
      data: { parts: [{ type: 'text', content: 'AI_old 部分内容', sequence: 0 }], full_content: 'AI_old 部分内容' },
    }))

    // 4. ★ 期望：通知夹在 AI_old 和 AI_new 之间，不被 AI_new 挤到末尾
    const finalIds = ids()
    const oldIdx = finalIds.indexOf(AI_OLD)
    const nIdx = finalIds.indexOf(sysId)
    const newIdx = finalIds.indexOf(AI_NEW)
    expect(
      [oldIdx, nIdx, newIdx],
      `通知应夹在 AI_old 和 AI_new 之间。实际顺序: ${JSON.stringify(finalIds)}`,
    ).toEqual([oldIdx, nIdx, newIdx].sort((a, b) => a - b))  // 升序 = old < n < new
    expect(nIdx).toBeGreaterThan(oldIdx)
    expect(nIdx).toBeLessThan(newIdx)
  })

  // ── fix_20260705_notification_stuck_at_bottom_real_storage（initFromAPI 全量替换语义下重写）──
  // 流式期间 system(seq=67/68) 按到达顺序夹在 ai(66) 与 ai(69) 之间，该断言不变。
  // 但刷新（initFromAPI）后 API 不返回 system → 新语义丢弃 system，store 恰好等于 API。
  it('场景11: 真实存储 — 流式期间 system(67/68) 夹在 ai(66)/ai(69) 之间；刷新后 system 被丢弃', () => {
    // 流式期间已经按到达顺序收到：ai(66) → system(67) → system(68) → ai(69)
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 60 }),
      makeMsg('ai-64', { role: 'assistant', content: '触发器第1次', sequence: 64, status: 'completed' }),
      makeMsg('tool-65', { role: 'tool', content: 'task_submit', sequence: 65 }),
      makeMsg('ai-66', { role: 'assistant', content: '任务已派发', sequence: 66, status: 'completed' }),
    ])
    // 流式期间收到 system 通知（seq 67/68，后端权威）
    handleSystemNotification(notificationEvent('[系统通知] 子任务1完成', { sequence: 67 }))
    handleSystemNotification(notificationEvent('[系统通知] 子任务2完成', { sequence: 68 }))
    // 然后 ai(69) 流式到达
    const AI69 = 'msg_ai_69'
    handlers.handleStreamStart(evt('stream_start', { message_id: AI69, _threadId: THREAD_ID, sequence: 69 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI69, content: '任务完成啦' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI69,
      _threadId: THREAD_ID,
      final_sequence: 69,
      data: { parts: [{ type: 'text', content: '任务完成啦', sequence: 0 }], full_content: '任务完成啦' },
    }))

    // 流式期间顺序（到达顺序）：[..., ai-66, sys-67, sys-68, ai-69]
    const sysIds = systemIds()
    expect(sysIds.length, '流式期间应有 2 条 system 通知').toBe(2)
    const beforeRefresh = ids()
    const ai66Idx = beforeRefresh.indexOf('ai-66')
    const ai69Idx = beforeRefresh.indexOf(AI69)
    expect(ai66Idx, 'ai-66 应存在').toBeGreaterThan(-1)
    expect(ai69Idx, 'ai-69 应存在').toBeGreaterThan(-1)
    for (const id of sysIds) {
      const idx = beforeRefresh.indexOf(id)
      expect(idx, '流式期间 system 应夹在 ai(66) 和 ai(69) 之间').toBeGreaterThan(ai66Idx)
      expect(idx).toBeLessThan(ai69Idx)
    }

    // ★ 切 Tab 触发 initFromAPI（全量替换语义）：API 返回的 records 不含 system（缺 67/68）
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 60 }),
      makeMsg('ai-64', { role: 'assistant', content: '触发器第1次', sequence: 64, status: 'completed' }),
      makeMsg('tool-65', { role: 'tool', content: 'task_submit', sequence: 65 }),
      makeMsg('ai-66', { role: 'assistant', content: '任务已派发', sequence: 66, status: 'completed' }),
      makeMsg(AI69, { role: 'assistant', content: '任务完成啦', sequence: 69, status: 'completed' }),
    ])

    // ★ 核心断言（新语义）：刷新后 system 通知全部丢弃，store 恰好等于 API 返回的 5 条
    expect(systemIds(), '刷新后 system 通知应被丢弃（API 未返回）').toHaveLength(0)
    expect(ids()).toEqual(['user-1', 'ai-64', 'tool-65', 'ai-66', AI69])
  })

  // ── fix_20260708_system_notification_duplicate_on_refresh（initFromAPI 全量替换语义）──
  // 旧 bug：流式 system 气泡 id 与后端落库 record_id 不一致，刷新后流式气泡 + API 记录并存 = 两条。
  // 修复后事件 payload 与 track 落库共用后端生成的 record_id。
  //
  // 新语义下，无论 id 是否一致，initFromAPI 都会全量替换：本地流式气泡丢弃，只剩 API 版本。
  // 此场景验证刷新后 system 恰好只剩 API 那一条（同 record_id），user-1 也不重复。
  it('场景12: 流式 system + 刷新返回同 record_id 的 system 记录 → 只剩 API 版（不重复）', () => {
    // 冷启动历史
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // 触发器通知到达：事件带 record_id（后端 emit_notification 生成）
    const notifEvt = notificationEvent('[触发器通知] 延迟测试已触发', { sequence: 18 })
    const notifRecordId = notifEvt.data.record_id
    handleSystemNotification(notifEvt)

    // 流式期间：1 条 system 气泡，id == 后端 record_id
    expect(systemIds(), '流式期间应 1 条 system').toHaveLength(1)
    expect(systemIds()[0]).toBe(notifRecordId)

    // 刷新：后端已落库 system 记录，API 返回它（id = 同一个 record_id, seq=18）
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg(notifRecordId, {
        role: 'system',
        content: '[触发器通知] 延迟测试已触发',
        sequence: 18,
        status: 'completed',
      }),
    ])

    // ★ 核心断言：刷新后 system 消息只剩 1 条（initFromAPI 全量替换：本地流式气泡丢弃，只剩 API 版）
    expect(systemIds(), '刷新后 system 不应重复').toHaveLength(1)
    expect(systemIds()[0]).toBe(notifRecordId)
    const finalIds = ids()
    const userCount = finalIds.filter((id) => id === 'user-1').length
    expect(userCount, 'user-1 也不应重复').toBe(1)
  })

  // ── record_id 缺失直接拒绝（暴露后端 bug，不做兜底）─────────────────────
  it('场景13: 事件缺 record_id → 拒绝创建气泡（强制后端 emit_notification 生成 id）', () => {
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // 故意构造不带 record_id 的事件（模拟后端 bug）
    handleSystemNotification({
      data: {
        pipeline_id: PIPELINE_ID,
        content: '残缺事件',
        notification_id: 'sys_xxx_1',
        // 不带 record_id
      },
    })

    // 应被拒绝，不创建任何 system 气泡
    expect(systemIds(), '缺 record_id 的事件不应创建气泡').toHaveLength(0)
  })
})
