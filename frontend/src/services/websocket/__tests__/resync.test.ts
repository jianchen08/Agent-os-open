/** @feature FP-0.2.一 插件协议（resync_required 消费 + schema_changed 主动感知） @ci frontend-test */
/**
 * resync.ts 单元测试
 *
 * 验证 resync_required 事件消费链：
 * ① 事件触发 schema 重拉+重载（refreshPluginContributions）
 * ② 2s 防抖：窗口内连续事件只重载一次；窗口外各自触发
 * ③ 重载失败仅 warn 不抛出（不产生未捕获拒绝，不阻塞 WS 主流程）
 * ④ 进行中不重入；重复 init 幂等；dispose 后不再响应
 *
 * mock 方式参照 GlobalWebSocket.test.ts：hoisted 稳定状态 + vi.mock 工厂 +
 * vi.resetModules() 动态导入获取干净的模块单例。
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

// ── Mock 依赖（vi.hoisted 保证 resetModules 后 mock 状态仍稳定共享） ──

const mocks = vi.hoisted(() => {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  return {
    /** 模拟 GlobalWebSocket 的 handler 注册表 */
    handlers,
    /** GrowthLoop.refreshPluginContributions mock（既有重载链的导出口） */
    refreshPluginContributions: vi.fn(),
    /** logger.warn mock */
    warn: vi.fn(),
  }
})

vi.mock('../GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, handler: (data: unknown) => void) => {
      if (!mocks.handlers.has(event)) mocks.handlers.set(event, new Set())
      mocks.handlers.get(event)!.add(handler)
    },
    unsubscribe: (event: string, handler: (data: unknown) => void) => {
      mocks.handlers.get(event)?.delete(handler)
    },
  },
}))

vi.mock('@/services/modules/GrowthLoop', () => ({
  refreshPluginContributions: mocks.refreshPluginContributions,
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    websocket: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: mocks.warn,
      error: vi.fn(),
    },
  },
}))

/** 与 resync.ts 内 RESYNC_DEBOUNCE_MS 保持一致 */
const RESYNC_DEBOUNCE_MS = 2_000

/** 派发一条服务端事件到当前订阅者（模拟 GlobalWebSocket._emit） */
function emitServerEvent(event: string, data: unknown): void {
  const handlers = mocks.handlers.get(event)
  if (handlers) {
    for (const h of handlers) h(data)
  }
}

/** 重置模块注册表后动态导入，获取干净的 resync 模块状态 */
async function loadResync(): Promise<typeof import('../resync')> {
  vi.resetModules()
  return await import('../resync')
}

// ── 测试套件 ──

describe('resync_required 消费者', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.handlers.clear()
    mocks.refreshPluginContributions.mockReset()
    mocks.refreshPluginContributions.mockResolvedValue(undefined)
    mocks.warn.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ──────────────────────────────────────────────
  // 1. 事件 → 重拉 + 重载
  // ──────────────────────────────────────────────
  it('resync_required 事件应触发既有重载链 refreshPluginContributions', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    // 事件名与 WS_SERVER_EVENTS.RESYNC_REQUIRED 一致（GlobalWebSocket._emit 用同名）
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })

    // 防抖窗口内尚未触发
    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)
  })

  it('防抖窗口内不触发：1999ms 时仍未重载', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS - 1)

    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()
  })

  // ──────────────────────────────────────────────
  // 2. 防抖：窗口内合并、窗口外各自触发
  // ──────────────────────────────────────────────
  it('防抖：连续多次事件（含同一消息的双重 emit）只重载一次', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    // GlobalWebSocket 对同一 resync_required 消息会 emit 两次（显式 + 按 type 通用），
    // 重连风暴中还会连续到达——防抖窗口内全部合并。
    // 5 次事件共推进 4×300ms=1200ms < 2000ms 防抖窗口，全部落在同一窗口内。
    for (let i = 0; i < 5; i++) {
      emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required', seq: i })
      await vi.advanceTimersByTimeAsync(300)
    }

    // 防抖窗口锚定在首个事件（t=2000 到期），此刻（t=1200）尚未触发
    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)

    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)
  })

  it('防抖窗口外的两次事件各自触发一次重载', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)

    // 窗口已过，第二次事件应再次触发
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(2)
  })

  // ──────────────────────────────────────────────
  // 3. 失败处理：warn 不抛
  // ──────────────────────────────────────────────
  it('重载失败不抛出、不产生未捕获拒绝，仅 warn', async () => {
    mocks.refreshPluginContributions.mockRejectedValue(new Error('schema 拉取失败'))
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })

    // 若 _performResync 向外抛出/拒绝，这里会出现未捕获拒绝（unhandled rejection）
    // 导致本测试直接失败——能走到后续断言即证明失败被吞掉、未阻塞 WS 主流程
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS + 50)
    await vi.advanceTimersByTimeAsync(0)

    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)
    expect(mocks.warn).toHaveBeenCalled()
  })

  // ──────────────────────────────────────────────
  // 4. 重入保护
  // ──────────────────────────────────────────────
  it('重载进行中再次触发不并发执行', async () => {
    let resolveFirst!: () => void
    mocks.refreshPluginContributions.mockImplementation(
      () => new Promise<void>((resolve) => { resolveFirst = resolve }),
    )
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    // 第一次触发，重载挂起（进行中）
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)

    // 第一次仍在进行中，窗口外再派发事件 → 应被重入保护挡掉
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS + 1_000)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)

    // 完成第一次，后续事件恢复正常触发
    resolveFirst()
    await vi.advanceTimersByTimeAsync(0)
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(2)
  })

  // ──────────────────────────────────────────────
  // 5. 订阅生命周期
  // ──────────────────────────────────────────────
  it('重复 init 幂等：同一事件只重载一次（Set 去重）', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()
    initResyncOnSchema()

    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)

    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)
  })

  it('dispose 后不再响应事件，且取消未触发的防抖', async () => {
    const { initResyncOnSchema, disposeResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    // 事件已到达、防抖计时器挂起中 → dispose 应取消计时器
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    disposeResyncOnSchema()
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS + 1_000)

    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()

    // dispose 后再派发也不响应
    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS + 1_000)
    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()
  })

  // ──────────────────────────────────────────────
  // 6. schema 变更推送（剩余项清仓 D2：widget_event {schema, changed} 消费）
  // ──────────────────────────────────────────────
  it('widget_event(schema, changed) 应触发同一重载链（防抖后重载一次）', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    // 内核广播信封：{type:"widget_event", data:{widget_id, event, data}, sequence}
    emitServerEvent(WS_SERVER_EVENTS.WIDGET_EVENT, {
      type: 'widget_event',
      data: { widget_id: 'schema', event: 'changed', data: { plugin_id: 'p1' } },
      sequence: 42,
    })

    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)
    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)
  })

  it('非 schema 的 widget 事件（其它 widget_id / 其它 event）不触发重载', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    // 其它 widget 的事件（如指标快照推送）
    emitServerEvent(WS_SERVER_EVENTS.WIDGET_EVENT, {
      type: 'widget_event',
      data: { widget_id: 'metrics_tick', event: 'tick', data: {} },
      sequence: 43,
    })
    // schema widget 但 event 不是 changed
    emitServerEvent(WS_SERVER_EVENTS.WIDGET_EVENT, {
      type: 'widget_event',
      data: { widget_id: 'schema', event: 'tick', data: {} },
      sequence: 44,
    })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS + 1_000)

    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()
  })

  it('两个通道（resync_required + schema changed）落在同一防抖窗口只重载一次', async () => {
    const { initResyncOnSchema } = await loadResync()
    initResyncOnSchema()

    emitServerEvent(WS_SERVER_EVENTS.RESYNC_REQUIRED, { type: 'resync_required' })
    emitServerEvent(WS_SERVER_EVENTS.WIDGET_EVENT, {
      type: 'widget_event',
      data: { widget_id: 'schema', event: 'changed', data: {} },
      sequence: 45,
    })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS)

    expect(mocks.refreshPluginContributions).toHaveBeenCalledTimes(1)
  })

  it('dispose 后 widget_event(schema, changed) 也不再响应', async () => {
    const { initResyncOnSchema, disposeResyncOnSchema } = await loadResync()
    initResyncOnSchema()
    disposeResyncOnSchema()

    emitServerEvent(WS_SERVER_EVENTS.WIDGET_EVENT, {
      type: 'widget_event',
      data: { widget_id: 'schema', event: 'changed', data: {} },
      sequence: 46,
    })
    await vi.advanceTimersByTimeAsync(RESYNC_DEBOUNCE_MS + 1_000)

    expect(mocks.refreshPluginContributions).not.toHaveBeenCalled()
  })
})
