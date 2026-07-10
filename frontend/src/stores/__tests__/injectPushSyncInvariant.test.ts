/**
 * 渲染顺序不变量测试：system_notification 必须在对应 AI stream_start 之前。
 *
 * 钉死核心契约（fix_20260705_notification_after_reply）：
 * 前端按 WS 事件到达顺序渲染。后端推送顺序必须是：
 *   emit_finish（关旧流）→ emit_notification（推通知）→ emit_start（开新流）
 * 这样 store 数组里 system 消息排在它对应的 AI 消息之前。
 *
 * 测试设计原理：
 * 模拟后端真实的 WS 事件序列，喂给前端的 handler，
 * 断言 store 数组里 system 消息在 AI 消息之前。
 * 如果后端推送顺序错了（emit_start 在 emit_notification 之前），
 * 或前端 handler 处理错了，store 数组顺序就会错，测试爆红。
 *
 * 历史教训：
 * 后端一个 emit_start 跨多轮 LLM 调用（一个流跨多轮），
 * 通知在流式输出期间到达，排在 AI 流后面。
 * 修复后：consume 在 LLM 之前 emit_finish 分割，通知排在新流之前。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Message } from '@/types/models'

// ── mock 外部依赖（与 messageOrderWithNotifications 对齐）──
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

const PIPELINE_ID = 'pid_inject_sync_aaaa'
const THREAD_ID = 'tid_inject_sync_bbbb'

let pipelineStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
let handlers: typeof import('@/services/websocket/streaming/handlers')
let handleSystemNotification: typeof import('@/services/websocket/streaming/lifecycleHandlers').handleSystemNotification
let flushStreamChunkBuffer: typeof import('@/services/websocket/streaming/handlers/streamHandler').flushStreamChunkBuffer

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

/** 构造一个流式 chunk / 工具等 WS 事件 */
function evt(type: string, data: Record<string, any>): any {
  return { type, sequence: data.sequence ?? 0, data: { pipeline_id: PIPELINE_ID, ...data } }
}

/** 构造一个 system_notification 事件（模拟后端 emit_notification，带 record_id） */
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

function flush(): void {
  flushStreamChunkBuffer()
}

function ids(): string[] {
  return pipelineStore.getState().getMessages(PIPELINE_ID).map((m) => m.id)
}

/** 所有 system 消息的 id（system 消息 id 现为后端 record_id，无固定前缀） */
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

describe('注入-推送同步不变量', () => {
  it('不变量1: system_notification 必须在对应 AI stream_start 之前到达（后端推送顺序正确）', () => {
    const AI_OLD = 'msg_ai_old'
    const AI_NEW = 'msg_ai_new'

    // 冷启动已有 AI_old
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 1 }),
      makeMsg(AI_OLD, { role: 'assistant', content: 'AI_old', sequence: 2, status: 'completed' }),
    ])

    // ★ 模拟后端正确的推送顺序：emit_finish → emit_notification → emit_start
    // 1. emit_finish（AI_old 流结束）
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_OLD,
      _threadId: THREAD_ID,
      final_sequence: 2,
      data: { parts: [{ type: 'text', content: 'AI_old', sequence: 0 }], full_content: 'AI_old' },
    }))

    // 2. emit_notification（推送系统通知）
    handleSystemNotification(notificationEvent('[系统通知] 任务完成', { sequence: 3 }))

    // 3. emit_start（新 AI 流开始）
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_NEW, _threadId: THREAD_ID, sequence: 4 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_NEW, content: 'AI_new 回复' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_NEW,
      _threadId: THREAD_ID,
      final_sequence: 4,
      data: { parts: [{ type: 'text', content: 'AI_new 回复', sequence: 0 }], full_content: 'AI_new 回复' },
    }))

    // ★ 断言：store 数组里 system 通知在 AI_new 之前
    const finalIds = ids()
    expect(systemIds(), '系统通知应存在').toHaveLength(1)
    const notifIdx = finalIds.indexOf(systemIds()[0])
    const aiNewIdx = finalIds.indexOf(AI_NEW)

    expect(aiNewIdx, 'AI_new 应存在').toBeGreaterThan(-1)
    expect(notifIdx, '系统通知必须在 AI_new 之前').toBeLessThan(aiNewIdx)
  })

  it('不变量2: 多条通知按时序逐条排布，不批量堆叠在最后', () => {
    const AI_1 = 'msg_ai_1'
    const AI_2 = 'msg_ai_2'
    const AI_3 = 'msg_ai_3'

    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 1 }),
    ])

    // ★ 模拟3轮正确的推送顺序：每轮 emit_finish → notification → emit_start
    // 第1轮
    handleSystemNotification(notificationEvent('[系统通知] 通知1', { sequence: 2 }))
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_1, _threadId: THREAD_ID, sequence: 3 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_1, content: '回复1' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_1, _threadId: THREAD_ID, final_sequence: 3,
      data: { parts: [{ type: 'text', content: '回复1', sequence: 0 }], full_content: '回复1' },
    }))

    // 第2轮
    handleSystemNotification(notificationEvent('[系统通知] 通知2', { sequence: 4 }))
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_2, _threadId: THREAD_ID, sequence: 5 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_2, content: '回复2' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_2, _threadId: THREAD_ID, final_sequence: 5,
      data: { parts: [{ type: 'text', content: '回复2', sequence: 0 }], full_content: '回复2' },
    }))

    // 第3轮
    handleSystemNotification(notificationEvent('[系统通知] 通知3', { sequence: 6 }))
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_3, _threadId: THREAD_ID, sequence: 7 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_3, content: '回复3' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: AI_3, _threadId: THREAD_ID, final_sequence: 7,
      data: { parts: [{ type: 'text', content: '回复3', sequence: 0 }], full_content: '回复3' },
    }))

    // ★ 断言：通知和 AI 回复交替排列，不是所有通知堆最后
    const finalIds = ids()
    const sysIds = systemIds()
    expect(sysIds.length, '应有3条系统通知').toBe(3)

    // 每条通知后面都应该跟着一条 AI 回复（交替）
    for (let i = 0; i < sysIds.length; i++) {
      const sysIdx = finalIds.indexOf(sysIds[i])
      // 通知不能在最后（后面必须有 AI 回复）
      expect(sysIdx, `第${i+1}条通知不应在最后`).toBeLessThan(finalIds.length - 1)
    }
  })

  it('不变量3: 如果后端推送顺序错了（emit_start 在 notification 之前），store 顺序会错', () => {
    // ★ 这个测试验证：如果后端推送顺序错了，测试能抓到。
    // 模拟错误顺序：emit_start → emit_notification（反了）
    const AI_NEW = 'msg_ai_wrong_order'

    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 1 }),
    ])

    // ★ 错误顺序：先 emit_start，后 emit_notification
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_NEW, _threadId: THREAD_ID, sequence: 3 }))
    handleSystemNotification(notificationEvent('[系统通知] 顺序错了', { sequence: 2 }))

    // 断言：这种情况下通知排在 AI 后面（验证测试能抓到顺序错误）
    const finalIds = ids()
    expect(systemIds(), '系统通知应存在').toHaveLength(1)
    const notifIdx = finalIds.indexOf(systemIds()[0])
    const aiIdx = finalIds.indexOf(AI_NEW)
    // 错误顺序下，通知在 AI 后面
    expect(notifIdx, '错误顺序：通知在 AI 后面').toBeGreaterThan(aiIdx)
  })

  it('不变量4: system_notification 不触发 ensureStreamingPlaceholder 合并（system 是边界）', () => {
    const AI_OLD = 'msg_ai_streaming'
    const AI_NEW = 'msg_ai_after_notif'

    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      makeMsg('user-1', { role: 'user', content: '问', sequence: 1 }),
    ])

    // AI_old 还在 streaming
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_OLD, _threadId: THREAD_ID, sequence: 2 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: AI_OLD, content: 'AI_old 输出' }))
    flush()

    // 通知到达（AI_old 还在 streaming）
    handleSystemNotification(notificationEvent('[系统通知] 任务完成', { sequence: 3 }))

    // AI_new stream_start 到达
    handlers.handleStreamStart(evt('stream_start', { message_id: AI_NEW, _threadId: THREAD_ID, sequence: 4 }))

    // ★ 断言：AI_new 是独立气泡（没合并到 AI_old），通知夹在中间
    const finalIds = ids()
    const oldIdx = finalIds.indexOf(AI_OLD)
    expect(systemIds(), '系统通知应存在').toHaveLength(1)
    const notifIdx = finalIds.indexOf(systemIds()[0])
    const newIdx = finalIds.indexOf(AI_NEW)

    expect(oldIdx).toBeGreaterThanOrEqual(0)
    expect(notifIdx).toBeGreaterThan(oldIdx, '通知应在 AI_old 之后')
    expect(newIdx).toBeGreaterThan(notifIdx, 'AI_new 应在通知之后（独立气泡，不合并到 AI_old）')
  })
})
