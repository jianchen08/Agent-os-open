/**
 * 系统通知 + 注入消息 + 刷新复杂场景的消息顺序测试。
 *
 * 验证 fix_20260627_message_order_jump_top：
 * 流式期间触发 initFromAPI（WS 重连补漏 / 切 Tab / 会话切换）时，
 * localOnly（流式占位 / optimistic grace）必须保持到达顺序追加到 API 权威消息末尾，
 * 不能按 sequence 归并——否则 sequence 不可靠的占位/grace 消息会错位插入，
 * 表现为「偶尔下一条输出跑到消息最上面」。
 *
 * 设计原则（与 multiturnOrderE2E 对齐）：
 * - 用真实 pipelineMessageStore + 真实 handlers（不 mock store）
 * - API 权威消息 sequence 可靠（≥1 单调），localOnly sequence 不可靠（前端自算 localMax+1）
 * - 渲染顺序 = store 数组顺序 = 到达顺序（API 已排序在前，localOnly 按到达序在后）
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
  it('场景1: 流式占位 localMax+1 与 API 权威 sequence 错位时，刷新后占位仍在末尾', () => {
    const MSG = 'msg_streaming_a000000'

    // 历史消息（API 风格）：user1(seq=1) → ai1(seq=2)
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2, parts: [{ type: 'text', content: '答1', sequence: 1 } as any] }),
    ])
    // 当前 store: [user-1, ai-1]，localMax(seq) = 2

    // 流式开始：stream_start 不带 sequence → 占位 sequence 走 localMax+1 = 3
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG, _threadId: THREAD_ID }))
    // 占位还在 streaming，此时刷新（initFromAPI）
    // 后端真实时序：ai1 之后注入了一条「上级」user 消息（seq=3），流式占位后端还没落库
    // → API 返回 user-injected(seq=3)，占位 sequence(3) 与它撞了，但占位不在 API 列表里
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2, parts: [{ type: 'text', content: '答1', sequence: 1 } as any] }),
      makeMsg('user-injected', { role: 'user', content: '[上级]继续', sequence: 3 }),
    ])

    // 期望：API 三条按 sequence 排序在前，流式占位保持到达顺序在末尾
    // 而不是把占位(sequence=3) 按 sequence 归并插到 user-injected(seq=3) 旁边/前面
    expect(ids()).toEqual(['user-1', 'ai-1', 'user-injected', MSG])
  })

  it('场景2: 系统通知 + 流式占位 + 切 Tab 刷新，占位保持到达顺序在末尾', () => {
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
    expect(systemIds(), '应有一条 system 通知').toHaveLength(1)
    const sysId = systemIds()[0]
    expect(beforeIds[beforeIds.length - 1]).toBe(sysId)

    // 切 Tab 触发 initFromAPI：API 只返回 user-1
    // 系统通知是 AI 消息之间的结构分隔符，必须保留：即使后端不在历史 API 中返回，
    // 刷新/切 Tab 后 system 仍留在列表里，作为前后 AI 气泡的边界，避免被错误合并。
    // 流式占位（assistant streaming）与 system 一起进 localOnly，保持到达顺序追加到 API 末尾。
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
    ])

    // 期望：API user-1 在前，localOnly（占位 + system）按到达顺序在末尾
    expect(ids()).toEqual(['user-1', MSG, sysId])
  })

  it('场景3: 上级注入 user 消息占 sequence + 流式占位，刷新后不互相覆盖、顺序正确', () => {
    const MSG = 'msg_streaming_c000000'

    // store 已有 user-1(seq=1) ai-1(seq=2)，localMax=2
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

    // 后端真实时序：后端把 user-2 落库 seq=3，但中间插入了上级 user-injected(后端 seq=4)，
    // 流式占位后端 seq=5 尚未落库 → API 返回到 seq=4
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
      makeMsg('user-2', { role: 'user', content: '问2', sequence: 3, clientMessageId: 'user-2' }),
      makeMsg('user-injected', { role: 'user', content: '[上级]补充', sequence: 4 }),
    ])

    // 期望：API 四条按 sequence 排序，流式占位(前端 seq=4) 保持到达顺序在末尾
    // 不被按 sequence(4) 归并到 user-injected(seq=4) 旁边
    expect(ids()).toEqual(['user-1', 'ai-1', 'user-2', 'user-injected', MSG])
  })

  it('场景4: optimistic grace user 消息 + 流式，刷新后 grace 消息保持到达顺序', () => {
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

    // 刷新：后端尚未持久化 user-2 和占位 → API 只返回到 ai-1
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
    ])

    // localOnly = [user-2(grace), MSG(streaming)]，保持到达顺序追加到 API 之后
    expect(ids()).toEqual(['user-1', 'ai-1', 'user-2', MSG])
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

  // ── fix_20260705_notification_after_reply ──────────────────────────────
  // 复现「系统通知跑到 AI 回复后面」的精确场景。
  //
  // 后端时序（bridge_core.py / engine_iteration.py 验证）：
  //   consume_pending_notifications 在 LLM 调用前调 emit_notification：
  //     1) emit_notification 推送 system_notification(seq=11, 后端权威)
  //     2) AI 流式输出 stream_start/end(seq=12, 同一计数器递增)
  // 后端 sequence 来自 _entry.next_sequence() 共享计数器，通知 seq=11 < AI seq=12，
  // 逻辑上通知必须在 AI 回复之前。
  //
  // 但前端 allocateNextSequence 对 system_notification 走 Math.max(backendSeq, localMax+1)，
  // 若通知事件 dispatch 时 localMax 已被流式推高，通知 sequence 被抬到 localMax+1，
  // initFromAPI 重排时被 mergeSorted 排到 AI 之后 → UI 显示「通知在查看结果后面」。
  //
  // B2 改动（addMessage 不推 bottomCursor）已防止游标污染，但排序仍依赖 sequence。
  // 本测试驱动「让带后端权威 sequence 的系统通知正确归位」的方案落地。
  it('场景7: 系统通知(后端权威 seq=11) 应排在 AI 回复(seq=12) 之前，刷新后不丢失', () => {
    const AI_MSG = 'msg_ai_after_notif'

    // 冷启动已有历史：user-1(seq=1) ai-1(seq=2)，localMax=2，bottomCursor=2（权威）
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
      sequence: 12, // stream_start 也可携带 sequence（stream_end 的 final_sequence 会在结束时同步）
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
    expect(systemIds(), '系统通知应已创建').toHaveLength(1)
    const sysId = systemIds()[0]
    const sysIdx = duringIds.indexOf(sysId)
    const aiIdx = duringIds.indexOf(AI_MSG)
    expect(aiIdx, 'AI 回复应已落定').toBeGreaterThan(-1)
    // ★ 第一断言：流式期间通知应在 AI 前面（到达顺序）
    expect(sysIdx, '流式期间：通知(先到) 应排在 AI(后到) 之前').toBeLessThan(aiIdx)

    // 切 Tab 触发 initFromAPI：后端历史 API 只返回落库的 user-1/ai-1 + AI 回复(seq=12)
    // 系统通知未落库 → localOnly 保留；其 sequence 必须保持后端权威值 11（不被抬升）
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
      makeMsg(AI_MSG, { role: 'assistant', content: '查报告', sequence: 12, status: 'completed' }),
    ])

    // ★ 第二断言（核心）：刷新后通知(seq=11) 必须仍在 AI(seq=12) 之前
    //    修复前：通知 sequence 被前端抬到 localMax+1=13，重排后跑到 AI(12) 后面 → bug
    //    修复后：通知 sequence 保持后端权威值 11，mergeSorted 按 sequence 归并 → 正确在前
    const finalIds = ids()
    const finalSysIdx = finalIds.indexOf(sysId!)
    const finalAiIdx = finalIds.indexOf(AI_MSG)
    expect(finalSysIdx, '刷新后通知应保留').toBeGreaterThan(-1)
    expect(finalAiIdx, '刷新后 AI 应保留').toBeGreaterThan(-1)
    expect(finalSysIdx, '刷新后：通知(seq=11) 必须排在 AI(seq=12) 之前').toBeLessThan(finalAiIdx)
  })

  // ── fix_20260705_notification_after_reply ──────────────────────────────
  // 复现「系统通知跑到 AI 回复后面」的根因场景：allocateNextSequence 抬升。
  //
  // 后端时序（同一 sequence 计数器递增）：
  //   子任务完成 → consume_pending_notifications 推送 system_notification(seq=11)
  //   但 WS 事件到达前端时，AI 的工具调用流式（tool_start/tool_result 等）
  //   可能已先把本地 localMax 推到更高（如 seq=15、16、17...）。
  //   此时通知事件才被 dispatch，allocateNextSequence(pipeline, 11) 走
  //   Math.max(11, localMax+1=18) = 18 → 通知 sequence 被抬到 18。
  //   initFromAPI 重排时，mergeSorted 把通知(seq=18) 排到 AI 主回复(seq=12) 之后。
  //
  // 本场景直接用 addMessage 模拟"流式工具调用已把 localMax 推高"的状态，
  // 再让系统通知"延迟到达"，精确复现 allocateNextSequence 抬升。
  it('场景8: 通知事件延迟到达 + localMax 已被推高时，通知 sequence 不应被抬升到 AI 之后', () => {
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

    // ★ 关键：模拟"通知事件延迟到达"——后端实际在 AI 主回复之前就推送了
    // （consume_pending_notifications 在 LLM 调用前 emit_notification），
    // 但 WS 分发 / handler 执行时序使它晚于 AI 主回复被处理。
    // 此时 localMax 已是 12（AI 主回复），allocateNextSequence(pipeline, 11)
    // 走 Math.max(11, 13) = 13 → 通知 sequence 被抬到 13，大于 AI 的 12。
    handleSystemNotification(notificationEvent('[系统通知] 子任务已完成', {
      sequence: 11, // 后端权威值，逻辑上早于 AI 主回复(12)
    }))

    expect(systemIds(), '系统通知应已创建').toHaveLength(1)
    const sysId = systemIds()[0]

    // 切 Tab 触发 initFromAPI：API 返回落库的 user-1/ai-1 + AI 主回复(seq=12)
    // 系统通知 localOnly 保留
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问1', sequence: 1 }),
      makeMsg('ai-1', { role: 'assistant', content: '答1', sequence: 2 }),
      makeMsg(AI_MAIN, { role: 'assistant', content: '先看报告', sequence: 12, status: 'completed' }),
    ])

    // ★ 核心断言：通知(seq=11, 后端权威) 必须排在 AI 主回复(seq=12) 之前
    //   修复前：allocateNextSequence 抬升通知 seq 到 13 → mergeSorted 排到 AI(12) 之后 → bug
    //   修复后：通知 seq 保持后端权威值 11 → 正确排在 AI 之前
    const finalIds = ids()
    const finalSysIdx = finalIds.indexOf(sysId!)
    const finalAiIdx = finalIds.indexOf(AI_MAIN)
    expect(finalSysIdx, '刷新后通知应保留').toBeGreaterThan(-1)
    expect(finalAiIdx, '刷新后 AI 应保留').toBeGreaterThan(-1)
    expect(
      finalSysIdx,
      `刷新后：通知(后端 seq=11) 必须排在 AI(seq=12) 之前。实际顺序: ${JSON.stringify(finalIds)}`,
    ).toBeLessThan(finalAiIdx)
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

  // ── fix_20260705_notification_stuck_at_bottom_real_storage ─────────────
  // 基于真实存储数据复现：system 通知不落库（track 不写 record），sequence 在存储里
  // 是"缺失"的（如 seq 64→69 缺 67/68，那是两条 system 通知）。
  // 前端流式期间 push 顺序到达 system（seq=67/68），它们排在 ai(seq=66) 之后、ai(seq=69) 之前，
  // 这是对的。但切 Tab/重连触发 initFromAPI 时：
  //   - API 返回的 records 不含 system（缺 67/68）
  //   - system 走 localOnly 保留，sequence 是后端权威的 67/68
  //   - mergeSorted 应按 sequence 归并，把 system(67/68) 插到 ai(66) 和 ai(69) 之间
  // 如果 system 被排到末尾，说明归并失败（sequence 抬升 或 localOnly 末尾拼接逻辑覆盖了归并）。
  it('场景11: 真实存储 — system(seq=67/68 缺失) 应在 initFromAPI 后正确归并到 ai(66) 和 ai(69) 之间', () => {
    // 流式期间已经按到达顺序收到：ai(66) → system(67) → system(68) → ai(69)
    // 模拟流式阶段
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
    const beforeRefresh = ids()
    const sysIds = systemIds()
    expect(sysIds.length, '应有 2 条 system 通知').toBe(2)

    // ★ 切 Tab 触发 initFromAPI：API 返回的 records 不含 system（缺 67/68）
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 60 }),
      makeMsg('ai-64', { role: 'assistant', content: '触发器第1次', sequence: 64, status: 'completed' }),
      makeMsg('tool-65', { role: 'tool', content: 'task_submit', sequence: 65 }),
      makeMsg('ai-66', { role: 'assistant', content: '任务已派发', sequence: 66, status: 'completed' }),
      makeMsg(AI69, { role: 'assistant', content: '任务完成啦', sequence: 69, status: 'completed' }),
    ])

    // ★ 核心断言：刷新后 system(67/68) 必须归并到 ai(66) 和 ai(69) 之间，不是末尾
    const finalIds = ids()
    const finalSysIds = systemIds()
    const ai66Idx = finalIds.indexOf('ai-66')
    const ai69Idx = finalIds.indexOf(AI69)
    const sysIdxes = finalSysIds.map((id) => finalIds.indexOf(id))

    expect(ai66Idx, 'ai-66 应存在').toBeGreaterThan(-1)
    expect(ai69Idx, 'ai-69 应存在').toBeGreaterThan(-1)
    expect(sysIdxes.length, 'system 通知应保留').toBe(2)
    for (const idx of sysIdxes) {
      expect(
        idx,
        `system(seq=67/68) 必须在 ai(66) 和 ai(69) 之间，不能在末尾。实际: ${JSON.stringify(finalIds)}`,
      ).toBeGreaterThan(ai66Idx)
      expect(idx).toBeLessThan(ai69Idx)
    }
  })

  // ── fix_20260708_system_notification_duplicate_on_refresh ──────────────
  // 复现「触发器通知刷新后渲染两次」。根因：流式 system 气泡 id 与后端落库
  // record_id 不一致（前端曾用 sys_<uuid>，后端 record_id 随机生成），刷新后
  // isCoveredByApi 按 id 去重失败 → 流式气泡 + API 记录并存 = 两条。
  //
  // 修复：后端 emit_notification 生成 record_id（唯一 id 来源），事件 payload 与
  // track 落库共用它；前端用 record_id 作消息 id。刷新后 API 返回同 record_id 的
  // system 记录 → isCoveredByApi 按 id 命中 → 本地流式气泡让位 API 版 → 只剩一条。
  it('场景12: 流式 system(后端 record_id) + 刷新返回同 record_id 的 system 记录 → 只剩一条', () => {
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

    // ★ 核心断言：刷新后 system 消息只剩 1 条（流式气泡被 API 同 id 版本覆盖，不并存）
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
