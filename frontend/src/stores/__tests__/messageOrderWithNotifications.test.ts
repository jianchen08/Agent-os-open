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

/** 构造一个 system_notification 事件（resolvePipelineId 取 data.pipeline_id） */
function notificationEvent(content: string, overrides: Record<string, any> = {}): any {
  return {
    data: {
      pipeline_id: PIPELINE_ID,
      content,
      level: 'info',
      notification_id: `sys_${Math.random().toString(36).slice(2, 10)}`,
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
    expect(beforeIds[beforeIds.length - 1]).toMatch(/^sys_/)
    const sysId = beforeIds[beforeIds.length - 1] as string

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
})
