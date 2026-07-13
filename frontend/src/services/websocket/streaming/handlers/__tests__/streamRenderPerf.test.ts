/**
 * 流式渲染性能量化测试
 *
 * 定位"转圈后一次弹出"的阻塞源：流式期间每个 chunk flush 触发的 store 更新
 * + 选择器判定开销，是否会随历史消息数量增长而恶化到阻塞主线程（RAF 被推迟）。
 *
 * 核心机制（已通过代码确认）：
 *  - appendToPart 每次 set 都 [...pipelineMessages] 重建数组 + {...msg, parts} 重建消息对象
 *  - ChatContainer 的 pipelineMessages 选择器用自定义 equalityFn 逐元素比对
 *  - 流式那条消息每帧引用都变 → equalityFn O(n) 遍历整个列表才返回 false → 触发重渲染
 *
 * 本测试驱动真实 store + 真实 appendToPart，量化：
 *  1. 单次 appendToPart 的 wall-clock 耗时随历史消息数（10/50/100/200）的变化
 *  2. 选择器 equalityFn 在"流式消息变了"场景的判定耗时
 *
 * 判据：
 *  - 若耗时随消息数近似线性增长，且在 ~100 条时单次 flush 接近/超过 16ms（一帧），
 *    即为阻塞源 —— 多个 chunk 积压，RAF 被推迟，表现"转圈后一次弹出"
 *  - 若耗时恒定且很小，则阻塞源不在此，需另查（MarkdownRenderer/其它同步任务）
 *
 * 注意：jsdom 无 JIT 预热，绝对值比生产偏慢，重点看【相对趋势】与【是否破 16ms 帧预算】。
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

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

const PIPELINE_ID = 'pipe-perf-001'
const STREAM_MSG_ID = 'msg-streaming-001'
const THREAD_ID = 'thread-perf-001'

/** 生成 N 条历史消息（已完成 assistant 消息，模拟长会话） */
function seedHistoryMessages(n: number) {
  const msgs: any[] = []
  for (let i = 0; i < n; i++) {
    msgs.push({
      id: `hist-${i}`,
      sessionId: THREAD_ID,
      role: 'assistant',
      content: `历史回复 #${i}：`.repeat(5),
      parts: [{ type: 'text', content: `历史回复 #${i}`, state: 'done', sequence: i }],
      timestamp: new Date(Date.now() - (n - i) * 1000).toISOString(),
      sequence: i,
      status: 'completed',
    })
  }
  return msgs
}

/** 复刻 ChatContainer 的 pipelineMessages 选择器 equalityFn（逐元素引用比对） */
function chatContainerEquality(a: any, b: any): boolean {
  if (a === b) return true
  if (!Array.isArray(a) || !Array.isArray(b)) return false
  if (a.length !== b.length) return false
  if (a.length === 0 && b.length === 0) return true
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false
  }
  return true
}

describe('流式渲染性能：store 更新开销 vs 历史消息数', () => {
  let usePipelineMessageStore: any

  beforeEach(async () => {
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /**
   * 在给定历史消息数下，量化：
   *  - 连续 M 次 appendToPart 的总耗时（模拟 M 个 chunk flush）
   *  - 每次 flush 后 ChatContainer equalityFn 的判定耗时
   */
  function measureWithHistory(historyCount: number, chunkCount: number) {
    usePipelineMessageStore.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {}, activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    // 灌入历史 + 一条流式占位消息（含一个 streaming text part）
    store.initFromAPI(PIPELINE_ID, seedHistoryMessages(historyCount))
    store.addMessage(PIPELINE_ID, {
      id: STREAM_MSG_ID, sessionId: THREAD_ID, role: 'assistant',
      content: '', parts: [{ type: 'text', content: '', state: 'streaming', sequence: 9999 }],
      timestamp: new Date().toISOString(), sequence: 9999, status: 'streaming',
    })
    store.startStreaming(PIPELINE_ID, STREAM_MSG_ID)

    // 找到 streaming text part 的 index
    const partIndex = store.findStreamingPartIndex(PIPELINE_ID, STREAM_MSG_ID)

    // 预取 selector 基线（flush 前的 messages 数组）
    let prevMessages = usePipelineMessageStore.getState().messagesByPipeline[PIPELINE_ID]

    let appendTotalNs = 0
    let equalityTotalNs = 0
    let equalityTrueCount = 0

    for (let c = 0; c < chunkCount; c++) {
      const t0 = performance.now()
      store.appendToPart(PIPELINE_ID, STREAM_MSG_ID, partIndex, '字')
      const t1 = performance.now()
      appendTotalNs += (t1 - t0)

      const curMessages = usePipelineMessageStore.getState().messagesByPipeline[PIPELINE_ID]
      const e0 = performance.now()
      const equal = chatContainerEquality(prevMessages, curMessages)
      const e1 = performance.now()
      equalityTotalNs += (e1 - e0)
      if (equal) equalityTrueCount++
      prevMessages = curMessages
    }

    return {
      historyCount,
      chunkCount,
      appendAvgMs: appendTotalNs / chunkCount,
      equalityAvgMs: equalityTotalNs / chunkCount,
      equalityTrueCount,
      frameBudgetExceeded: (appendTotalNs + equalityTotalNs) / chunkCount > 16,
    }
  }

  it('量化不同历史消息数下的单帧 flush 开销', () => {
    const results: any[] = []
    for (const n of [10, 50, 100, 200, 300]) {
      results.push(measureWithHistory(n, 30))
    }

    console.log('\n========== 流式 flush 开销 vs 历史消息数 ==========')
    console.log('帧预算 = 16ms/帧；超过即 RAF 被推迟 → chunk 积压')
    console.table(results.map(r => ({
      '历史消息数': r.historyCount,
      '单次append(ms)': r.appendAvgMs.toFixed(3),
      '单次equality(ms)': r.equalityAvgMs.toFixed(3),
      '合计(ms/帧)': (r.appendAvgMs + r.equalityAvgMs).toFixed(3),
      '是否破帧预算': r.frameBudgetExceeded ? '⚠️ 是' : '否',
      'equality误判true次数': r.equalityTrueCount,
    })))

    // 趋势断言：append 耗时随消息数增长（数组重建 O(n)）
    const append10 = results[0].appendAvgMs
    const append300 = results[4].appendAvgMs
    console.log(`\nappend 耗时 10条→300条: ${append10.toFixed(3)}ms → ${append300.toFixed(3)}ms`)
    // 增长倍数应远大于 1（线性增长特征）
    expect(append300).toBeGreaterThan(append10 * 1.5)
  })

  it('模拟"积压爆发"：100条历史 + 60个chunk不flush，测量一次性flush的峰值耗时', () => {
    // 这个场景镜像生产：主线程被占住期间，chunk 在 RAF buffer 积压，
    // 主线程空闲后 _flushChunks 一次性把所有积压写入 store。
    // 这里直接量化"一次性写 60 次 appendToPart"的峰值耗时。
    usePipelineMessageStore.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {}, activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    store.initFromAPI(PIPELINE_ID, seedHistoryMessages(100))
    store.addMessage(PIPELINE_ID, {
      id: STREAM_MSG_ID, sessionId: THREAD_ID, role: 'assistant',
      content: '', parts: [{ type: 'text', content: '', state: 'streaming', sequence: 9999 }],
      timestamp: new Date().toISOString(), sequence: 9999, status: 'streaming',
    })
    const partIndex = store.findStreamingPartIndex(PIPELINE_ID, STREAM_MSG_ID)

    const t0 = performance.now()
    // 一次性 flush 60 个积压 chunk
    for (let c = 0; c < 60; c++) {
      store.appendToPart(PIPELINE_ID, STREAM_MSG_ID, partIndex, '字')
    }
    const elapsed = performance.now() - t0
    console.log(`\n[积压爆发] 100条历史 + 一次性flush 60个chunk: ${elapsed.toFixed(2)}ms`)
    console.log(`  → 如果此值接近/超过 16ms，证明积压 flush 本身也会造成卡顿`)

    expect(elapsed).toBeGreaterThan(0)
  })
})
