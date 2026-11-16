/** @feature FP-0.2.待发送队列 发送落定语义 | @ci: frontend-test
 * 发送态生命周期重定义（落定语义）store 契约测试
 *
 * 用户裁定（2026-11-04，设计真值 docs/working/聊天消息流权威化重设计_20261027.md
 * 不变量 2 零乐观气泡不变）：
 *   - 发送态终点 = 消息落进消息流（气泡认领/队列条出现），不是前端受理；
 *   - 发送态期间输入锁定；5s 未落定 → 判死（撤锁+timeout outcome+宿主停止链），
 *     判死即终局：迟到落定/20s 看门狗对同一 cmid 零动作；
 *   - 失败（ack ok:false / 20s ack 看门狗）= 另一判死出口（撤发送态+failed）；
 *   - 在途记录带输入快照：落定清空仅当当前文本 == 快照（不吞新输入）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type * as chatInputStoreMod from '@/stores/chatInputStore'

vi.mock('@/utils/logger', () => ({
  loggers: { storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

describe('chatInputStore - 发送落定语义', () => {
  let store: chatInputStoreMod.useChatInputStore

  beforeEach(async () => {
    localStorage.clear()
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-04T00:00:00Z'))
    vi.resetModules()
    store = (await import('@/stores/chatInputStore')).useChatInputStore
    store.setState({
      pendingInsert: null,
      drafts: {},
      sendInFlightByPipeline: {},
      sendOutcomes: [],
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /** 读取末尾 outcome 的助手 */
  const lastOutcome = () => {
    const outcomes = store.getState().sendOutcomes
    return outcomes[outcomes.length - 1]
  }

  it('导出 SEND_LANDING_TIMEOUT_MS = 5000（落定超时常量名即契约）', async () => {
    const mod = await import('@/stores/chatInputStore')
    expect(mod.SEND_LANDING_TIMEOUT_MS).toBe(5000)
  })

  it('beginSend 登记在途条目：content 快照 / startedAt / draftKey', () => {
    const before = Date.now()
    store.getState().beginSend('pipe-1', 'cmid-1', '输入快照', 'tab-1')
    const entries = store.getState().sendInFlightByPipeline['pipe-1']
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({
      clientMessageId: 'cmid-1',
      content: '输入快照',
      draftKey: 'tab-1',
    })
    expect(entries[0].startedAt).toBeGreaterThanOrEqual(before)
  })

  it('落定：landSendByCmid 撤在途 + landed outcome；快照一致时清草稿（清空只在落定）', () => {
    store.getState().saveDraft('tab-1', '你好')
    store.getState().beginSend('pipe-1', 'cmid-1', '你好', 'tab-1')

    store.getState().landSendByCmid('cmid-1')

    expect(store.getState().sendInFlightByPipeline['pipe-1']).toEqual([])
    expect(lastOutcome()).toMatchObject({
      clientMessageId: 'cmid-1',
      pipelineId: 'pipe-1',
      kind: 'landed',
      content: '你好',
      draftKey: 'tab-1',
    })
    // 快照一致 → 草稿随落定清除
    expect(store.getState().drafts['tab-1']).toBeUndefined()
  })

  it('快照比对保护：落定时草稿已被改（≠ 快照）→ 草稿保留（不吞新输入）', () => {
    store.getState().saveDraft('tab-2', '原文')
    store.getState().beginSend('pipe-1', 'cmid-2', '原文', 'tab-2')
    // 超时解锁后用户改字 → 草稿更新为新文本
    store.getState().saveDraft('tab-2', '原文加新字')

    store.getState().landSendByCmid('cmid-2')

    expect(lastOutcome()?.kind).toBe('landed')
    // 新输入不能被迟到的落定吞掉
    expect(store.getState().drafts['tab-2']).toBe('原文加新字')
  })

  it('5s 超时 → 判死：撤在途 + timeout outcome（终局）；迟到落定幂等零动作、不触碰草稿', () => {
    store.getState().saveDraft('tab-3', '慢消息')
    store.getState().beginSend('pipe-1', 'cmid-3', '慢消息', 'tab-3')

    vi.advanceTimersByTime(5000)

    // 判死即终局（用户裁定：超时=后端这条消息坏了）：在途撤下 + timeout outcome
    expect(store.getState().sendInFlightByPipeline['pipe-1']).toEqual([])
    expect(lastOutcome()).toMatchObject({ clientMessageId: 'cmid-3', kind: 'timeout' })

    // 判死后不产生 failed（同一 cmid 终局唯一）
    expect(store.getState().sendOutcomes.some((o) => o.kind === 'failed')).toBe(false)

    // 迟到的 confirmed 零动作：不产生第二个终局、不触碰草稿（文本归属用户重试）
    const count = store.getState().sendOutcomes.length
    store.getState().landSendByCmid('cmid-3')
    expect(store.getState().sendOutcomes).toHaveLength(count)
    expect(store.getState().drafts['tab-3']).toBe('慢消息')
  })

  it('判死终局唯一：5s 超时判死后，20s ack 看门狗幂等零动作（不重复宣告）', () => {
    store.getState().beginSend('pipe-1', 'cmid-two-stage', '两级消息', 'tab-two')

    vi.advanceTimersByTime(5000)
    expect(lastOutcome()?.kind).toBe('timeout')

    // 20s ack 看门狗到点：cmid 已终局，不产生第二个失败宣告（不重复报错）
    store.getState().endSendByCmid('cmid-two-stage', '已发送但 20s 内未收到服务器回执')
    expect(store.getState().sendOutcomes.some((o) => o.kind === 'failed')).toBe(false)
    expect(store.getState().sendOutcomes).toHaveLength(1)
  })

  it('多在途按计数：同管道两条，落定一条仍在途，全落定才清零', () => {
    store.getState().beginSend('pipe-1', 'cmid-a', '第一条')
    store.getState().beginSend('pipe-1', 'cmid-b', '第二条')

    store.getState().landSendByCmid('cmid-a')
    expect(store.getState().sendInFlightByPipeline['pipe-1']).toHaveLength(1)

    store.getState().landSendByCmid('cmid-b')
    expect(store.getState().sendInFlightByPipeline['pipe-1']).toEqual([])
  })

  it('失败语义：endSendByCmid(cmid, reason) 撤在途 + failed outcome 携 reason（判死出口），落定不再二次处理', () => {
    store.getState().beginSend('pipe-1', 'cmid-f', '失败消息', 'tab-f')

    store.getState().endSendByCmid('cmid-f', '已发送但 20s 内未收到服务器回执')

    expect(store.getState().sendInFlightByPipeline['pipe-1']).toEqual([])
    expect(lastOutcome()).toMatchObject({
      clientMessageId: 'cmid-f',
      kind: 'failed',
      reason: '已发送但 20s 内未收到服务器回执',
    })
    // 失败不产生 timeout，也不清草稿（文本未离开输入框，天然可重试）
    expect(store.getState().sendOutcomes.some((o) => o.kind === 'timeout')).toBe(false)

    // 同 cmid 后到的落定/失败幂等零动作（不产生第二个 outcome）
    const count = store.getState().sendOutcomes.length
    store.getState().landSendByCmid('cmid-f')
    store.getState().endSendByCmid('cmid-f', '重复判死')
    expect(store.getState().sendOutcomes).toHaveLength(count)
  })

  it('未知 cmid 的落定/失败幂等零动作（注入消息等非本端发送不误触）', () => {
    store.getState().landSendByCmid('never-registered')
    store.getState().endSendByCmid('never-registered', 'x')
    expect(store.getState().sendOutcomes).toEqual([])
    expect(store.getState().sendInFlightByPipeline).toEqual({})
  })
})
