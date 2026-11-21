/** @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P0感知闭环 | @vision V2 全能闭环 | @ci frontend-test */
/**
 * uiBridge 前端纯函数面验收测试（AC-P0-7/8，TDD Red——API 契约由本文件定义）
 *
 * 被测契约 = 需求冻结件 §2.3 逐字
 * （docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md；实现方对本件只读）：
 * - ../builtinItems.ts：SidebarItemDecl + BUILTIN_SIDEBAR_ITEMS
 *   （恒含 {id:'builtin.new_session', title:'新建会话'} / {id:'builtin.tasks', title:'任务管理'}）
 * - ../snapshotBuilder.ts：SidebarSnapshotInput + buildSidebarSnapshot
 *   （输出 = §2.1 除 client_id/ts 外全部字段；pluginPages 仅供 activity-bar 插件贡献页）
 * - ../stateReporter.ts：createReportScheduler
 *   （注入 send 与 now；debounceMs 默认 500；heartbeatMs 默认 30000；
 *     防抖窗口内多次 notifyChange 只 send 一次；onReconnect 立即 send；
 *     heartbeat 周期 send；send 抛错不外抛、于下一触发点自然重试）
 *
 * mock 仅限注入点：send（vi.fn 外发函数，冻结件唯一 mock 点）与 now（注入时钟，
 * 与 vitest fake timers 锁步推进——定时器唤醒与时钟读数同步走，不依赖全局 Date 打桩）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BUILTIN_SIDEBAR_ITEMS } from '../builtinItems'
import { buildSidebarSnapshot, type SidebarSnapshotInput } from '../snapshotBuilder'
import { createReportScheduler } from '../stateReporter'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

const pluginPage = (id: string, title: string, order: number): PageDeclaration => ({
  type: 'pages',
  id,
  title,
  space: 'chat',
  slot: 'activity-bar',
  order,
})

const baseInput = (): SidebarSnapshotInput => ({
  collapsed: false,
  widthRatio: 0.3,
  activeView: 'sessions',
  unreadCount: 2,
  pluginPages: [],
  sessions: [{ id: 'sess-1', title: '会话一', pinned: false, active: true }],
})

const ACTIONS_VOCAB = ['navigate', 'set_collapsed', 'open_page', 'highlight']

// ── AC-P0-7：buildSidebarSnapshot 输出面（声明/状态两面拼接）─────────────

describe('BUILTIN_SIDEBAR_ITEMS（§2.3 内置项声明表）', () => {
  it('恒含 builtin.new_session=新建会话 与 builtin.tasks=任务管理，字段齐全', () => {
    const ids = BUILTIN_SIDEBAR_ITEMS.map((item) => item.id)
    expect(ids).toContain('builtin.new_session')
    expect(ids).toContain('builtin.tasks')
    expect(BUILTIN_SIDEBAR_ITEMS.find((item) => item.id === 'builtin.new_session')?.title).toBe(
      '新建会话',
    )
    expect(BUILTIN_SIDEBAR_ITEMS.find((item) => item.id === 'builtin.tasks')?.title).toBe('任务管理')
    for (const item of BUILTIN_SIDEBAR_ITEMS) {
      expect(item.kind).toBe('builtin')
      expect(typeof item.order).toBe('number')
      expect(typeof item.visible).toBe('boolean')
    }
  })
})

describe('buildSidebarSnapshot（AC-P0-7）', () => {
  it('无插件页：state 4 字段 + items 恰为 builtin 两项 + actions 词表 4 项 + sessions 透传', () => {
    const snap = buildSidebarSnapshot(baseInput())

    // state 4 字段（输出面 = §2.1 snake_case：width_ratio/active_view/unread_count）
    expect(snap.state).toEqual({
      collapsed: false,
      width_ratio: 0.3,
      active_view: 'sessions',
      unread_count: 2,
    })

    // items = builtin 两项（id/title 逐项断言，不钉 order 具体值）
    expect(snap.items).toHaveLength(2)
    expect(snap.items.map((item) => item.id).sort()).toEqual([
      'builtin.new_session',
      'builtin.tasks',
    ])
    expect(snap.items.find((item) => item.id === 'builtin.new_session')?.title).toBe('新建会话')
    expect(snap.items.find((item) => item.id === 'builtin.tasks')?.title).toBe('任务管理')

    // actions 词表封闭 4 项
    expect([...snap.actions].sort()).toEqual([...ACTIONS_VOCAB].sort())

    // sessions 透传（字段四元组原样）
    expect(snap.sessions).toEqual([{ id: 'sess-1', title: '会话一', pinned: false, active: true }])
  })

  it('有插件页：全部 activity-bar 插件页进 items（kind=plugin_page），builtin 两项仍在', () => {
    const input: SidebarSnapshotInput = {
      ...baseInput(),
      pluginPages: [pluginPage('rp.writer', '写作台', 10), pluginPage('dsh.panel', 'DSH 面板', 20)],
    }
    const snap = buildSidebarSnapshot(input)

    const pageItems = snap.items.filter((item) => item.kind === 'plugin_page')
    expect(pageItems.map((item) => item.id).sort()).toEqual(['dsh.panel', 'rp.writer'])
    expect(snap.items.find((item) => item.id === 'rp.writer')?.title).toBe('写作台')
    // builtin 两项不被插件页挤出，items 总数 = 2 builtin + 2 plugin_page
    expect(
      snap.items.filter(
        (item) => item.id === 'builtin.new_session' || item.id === 'builtin.tasks',
      ),
    ).toHaveLength(2)
    expect(snap.items).toHaveLength(4)
    // actions 词表不受插件页影响
    expect(snap.actions).toHaveLength(4)
  })

  it('会话 60 条截断到 50（保留前 50 顺序）', () => {
    const manySessions = Array.from({ length: 60 }, (_, i) => ({
      id: `sess-${String(i + 1).padStart(2, '0')}`,
      title: `会话 ${i + 1}`,
      pinned: false,
      active: false,
    }))
    const snap = buildSidebarSnapshot({ ...baseInput(), sessions: manySessions })

    expect(snap.sessions).toHaveLength(50)
    expect(snap.sessions.map((s) => s.id)).toEqual(manySessions.slice(0, 50).map((s) => s.id))
  })
})

// ── AC-P0-8：createReportScheduler 防抖/重连/心跳/失败重试 ──────────────

describe('createReportScheduler（AC-P0-8）', () => {
  let clockNow: number
  /** 注入时钟（冻结件 §2.3 now）：与 vitest fake timers 锁步推进。 */
  const now = () => clockNow
  /** 同时推进注入时钟与定时器：调度器的唤醒（定时器）与读数（now）同步走。 */
  const advanceClock = async (ms: number) => {
    clockNow += ms
    await vi.advanceTimersByTimeAsync(ms)
  }

  beforeEach(() => {
    vi.useFakeTimers()
    clockNow = 1_000_000
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('默认防抖 500ms：窗口内 3 次 notifyChange → 窗口闭合一发；下一窗口再发', async () => {
    const send = vi.fn(async (_snapshot: object) => undefined)
    const sched = createReportScheduler({ send, now })

    sched.notifyChange()
    await advanceClock(100)
    sched.notifyChange()
    await advanceClock(100)
    sched.notifyChange() // 500ms 窗口内第 3 次：窗口随每次 notify 重置

    await advanceClock(499)
    expect(send).toHaveBeenCalledTimes(0)
    await advanceClock(1) // 距最后一次 notify 满 500ms
    expect(send).toHaveBeenCalledTimes(1)
    expect(send.mock.calls[0]).toHaveLength(1)
    expect(send.mock.calls[0][0]).toBeInstanceOf(Object)

    // 下一触发点（新窗口）自然再发——防抖不闩死
    sched.notifyChange()
    await advanceClock(500)
    expect(send).toHaveBeenCalledTimes(2)
    sched.stop()
  })

  it('debounceMs 可注入（200ms 窗口：199ms 未发、200ms 闭合即发）', async () => {
    const send = vi.fn(async (_snapshot: object) => undefined)
    const sched = createReportScheduler({ send, now, debounceMs: 200 })

    sched.notifyChange()
    await advanceClock(199)
    expect(send).toHaveBeenCalledTimes(0)
    await advanceClock(1)
    expect(send).toHaveBeenCalledTimes(1)
    sched.stop()
  })

  it('onReconnect 立即 send（不等防抖窗口闭合）', () => {
    const send = vi.fn(async (_snapshot: object) => undefined)
    const sched = createReportScheduler({ send, now })

    sched.notifyChange() // 防抖挂起中
    sched.onReconnect()
    expect(send).toHaveBeenCalledTimes(1) // 同步立即，未推进任何定时器
    sched.stop()
  })

  it('默认心跳 30000ms：无变更也周期 send', async () => {
    const send = vi.fn(async (_snapshot: object) => undefined)
    const sched = createReportScheduler({ send, now })

    sched.onReconnect() // 初始一发
    expect(send).toHaveBeenCalledTimes(1)

    await advanceClock(29_999)
    expect(send).toHaveBeenCalledTimes(1) // 心跳未到
    await advanceClock(1) // 满 30s
    expect(send).toHaveBeenCalledTimes(2)
    await advanceClock(30_000) // 周期持续
    expect(send).toHaveBeenCalledTimes(3)
    sched.stop()
  })

  it('heartbeatMs 可注入（1000ms 周期）', async () => {
    const send = vi.fn(async (_snapshot: object) => undefined)
    const sched = createReportScheduler({ send, now, heartbeatMs: 1000 })

    await advanceClock(999)
    expect(send).toHaveBeenCalledTimes(0)
    await advanceClock(1)
    expect(send).toHaveBeenCalledTimes(1)
    await advanceClock(1000)
    expect(send).toHaveBeenCalledTimes(2)
    sched.stop()
  })

  it('send 连续失败不外抛，下一触发点自然重试成功', async () => {
    const send = vi.fn(async (_snapshot: object) => undefined)
      .mockRejectedValueOnce(new Error('network down'))
      .mockRejectedValueOnce(new Error('network down'))
    const sched = createReportScheduler({ send, now })

    // 触发点 1：onReconnect → send 拒绝，错误不得外抛
    expect(() => sched.onReconnect()).not.toThrow()
    await vi.advanceTimersByTimeAsync(0) // 冲刷微任务：拒绝已被调度器吞掉
    expect(send).toHaveBeenCalledTimes(1)

    // 触发点 2：心跳周期到 → 第二次尝试仍失败，仍不外抛
    await advanceClock(30_000)
    expect(send).toHaveBeenCalledTimes(2)

    // 触发点 3：下一周期 → 第三次成功（mock 默认实现 resolve）
    await advanceClock(30_000)
    expect(send).toHaveBeenCalledTimes(3)
    sched.stop()
  })
})
