/** @feature FP-T12 前端适配（历史消息内存封顶） @vision V6 可即用 @ci frontend-test */
/**
 * prependMessages 内存封顶与翻页闸解耦测试（2026-10-13 用户裁定：cap 触发即
 * 关闸是设计缺陷——长会话滚到顶无法再看更早历史，内核侧全量完好）。
 *
 * 契约：
 * - 内存窗口 = 20000（仅极端增长兜底）；合并后超窗口 → 保留最新 20000；
 * - hasMoreOlder 唯一依据后端 has_more，cap 裁顶不再干预（禁关闸）；
 * - topCursor = 保留首条 sequence；
 * - >2000 条的长会话可连续向上翻页：不关闸、无重复、序严格递增。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as storeMod from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', async () =>
  (await import('./helpers/storeTestMocks')).loggerMockFull(),
)

const PIPE = 'pipe-prepend-cap-001'
const THREAD = 'thread-prepend-cap-001'

const LIMIT = 20000

function mkMsg(id: string, seq: number): Message {
  return {
    id,
    sessionId: THREAD,
    sequence: seq,
    role: 'user',
    content: `内容 ${seq}`,
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
  } as Message
}

function seqRange(from: number, to: number): Message[] {
  const out: Message[] = []
  for (let s = from; s <= to; s++) out.push(mkMsg(`m-${s}`, s))
  return out
}

describe('prependMessages 内存封顶与翻页闸', () => {
  let usePipelineMessageStore: storeMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPE]: THREAD },
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
  })

  it('超旧 2000 窗口（2000+50=2050 < 20000）：不裁剪，has_more 透传不关闸（原 2000 关闸缺陷回归）', () => {
    const store = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: { ...s.messagesByPipeline, [PIPE]: seqRange(51, 2050) },
    }))

    store.prependMessages(PIPE, seqRange(1, 50), true)

    const msgs = store.getMessages(PIPE)
    expect(msgs).toHaveLength(2050)
    expect(msgs[0].sequence).toBe(1)
    expect(msgs[2049].sequence).toBe(2050)
    expect(store.getTopCursor(PIPE)).toBe(1)
    // 核心：不再因容量截断关闭向上翻页闸
    expect(store.hasMoreOlder(PIPE)).toBe(true)
  })

  it('未超限：has_more 透传，topCursor 取更早消息首条', () => {
    const store = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: { ...s.messagesByPipeline, [PIPE]: seqRange(101, 200) },
    }))

    store.prependMessages(PIPE, seqRange(51, 100), true)

    const msgs = store.getMessages(PIPE)
    expect(msgs).toHaveLength(150)
    expect(msgs[0].sequence).toBe(51)
    expect(store.getTopCursor(PIPE)).toBe(51)
    expect(store.hasMoreOlder(PIPE)).toBe(true)
  })

  it('恰在内存窗口边界（合并后恰 20000）：不裁剪，has_more 照常透传', () => {
    const store = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: { ...s.messagesByPipeline, [PIPE]: seqRange(101, LIMIT) },
    }))

    store.prependMessages(PIPE, seqRange(1, 100), true)

    const msgs = store.getMessages(PIPE)
    expect(msgs).toHaveLength(LIMIT)
    expect(msgs[0].sequence).toBe(1)
    expect(msgs[LIMIT - 1].sequence).toBe(LIMIT)
    expect(store.getTopCursor(PIPE)).toBe(1)
    expect(store.hasMoreOlder(PIPE)).toBe(true)
  })

  it('超内存窗口（20000+50）：裁到 20000 保留最新，has_more 仍透传（不再 cap 关闸），topCursor=保留首条', () => {
    const store = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: { ...s.messagesByPipeline, [PIPE]: seqRange(51, 50 + LIMIT) },
    }))

    store.prependMessages(PIPE, seqRange(1, 50), true)

    const msgs = store.getMessages(PIPE)
    expect(msgs).toHaveLength(LIMIT)
    expect(msgs[0].sequence).toBe(51)
    expect(msgs[LIMIT - 1].sequence).toBe(50 + LIMIT)
    expect(store.getTopCursor(PIPE)).toBe(51)
    // cap 裁顶不干预翻页闸：has_more 唯一真值
    expect(store.hasMoreOlder(PIPE)).toBe(true)
  })

  it('后端明确无更多（has_more=false）：照常关闸（API 真值，非 cap 干预）', () => {
    const store = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: { ...s.messagesByPipeline, [PIPE]: seqRange(101, 200) },
    }))

    store.prependMessages(PIPE, seqRange(51, 100), false)

    expect(store.hasMoreOlder(PIPE)).toBe(false)
  })
})

describe('长会话连续向上翻页（>2000 条不关闸）', () => {
  let usePipelineMessageStore: storeMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPE]: THREAD },
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
  })

  it('多轮 prepend 持续加载更早页：闸保持开、无重复、sequence 严格递增', () => {
    const store = usePipelineMessageStore.getState()
    // 冷启动载入最新 2000 条（seq 1..2000 中的后 2000：51..2050）
    store.initFromAPI(PIPE, seqRange(51, 2050), true)
    expect(store.getMessages(PIPE)).toHaveLength(2000)
    expect(store.hasMoreOlder(PIPE)).toBe(true)

    // 模拟用户连续向上翻页 3 轮（每轮 50 条更早消息，后端 has_more 恒真）
    for (let round = 0; round < 3; round++) {
      const top = store.getTopCursor(PIPE)
      const pageFrom = top - 50
      store.prependMessages(PIPE, seqRange(pageFrom, top - 1), true)

      const msgs = store.getMessages(PIPE)
      // 闸保持开：上一轮 cap 关闸缺陷会让第 1 轮后即停
      expect(store.hasMoreOlder(PIPE)).toBe(true)
      // 无重复：id 集合大小 = 条数
      const ids = new Set(msgs.map((m) => m.id))
      expect(ids.size).toBe(msgs.length)
      // 不丢序：sequence 严格递增
      for (let i = 1; i < msgs.length; i++) {
        expect(msgs[i].sequence!).toBeGreaterThan(msgs[i - 1].sequence!)
      }
      // 每轮真实增长 50（未被裁剪、未被去重吞掉）
      expect(msgs).toHaveLength(2000 + (round + 1) * 50)
      expect(store.getTopCursor(PIPE)).toBe(pageFrom)
    }
  })
})
