// @feature FP-T12 前端适配(装机事故批次2 连接池自愈) | @ci: frontend-test
/**
 * 连接池毒化自愈·渲染侧请求服务测试（connectionSelfHeal.ts）。
 *
 * 行为契约：
 *  - 触发阈值：重连失败 < RECONNECT_DEGRADE_ATTEMPTS 不请求；达阈值请求，
 *    载荷 { reconnectAttempts }（IPC 两端字段对齐：主进程 handler 同名消费）；
 *  - 触发节拍：超阈值但非阈值整数倍不请求（第 10/20/30… 次才请求一轮），
 *    冷却限频由主进程承载，渲染侧零状态；
 *  - 无 electronAPI（浏览器/dev/移动壳）→ no-op 不炸；request 拒绝 →
 *    不产生 unhandled rejection（重连循环继续兜底）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RECONNECT_DEGRADE_ATTEMPTS } from '@/constants/websocket'
import {
  maybeRequestConnectionSelfHeal,
  subscribeConnectionRebuilt,
} from '../connectionSelfHeal'

type RequestPayload = { reconnectAttempts: number }

function installApi(requestImpl?: (p: RequestPayload) => Promise<unknown>): {
  request: ReturnType<typeof vi.fn>
  onRebuilt: ReturnType<typeof vi.fn>
} {
  const request = vi.fn(requestImpl ?? (async () => ({ rebuilt: true })))
  const onRebuilt = vi.fn(() => () => {})
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = {
    connectionSelfHeal: { request, onRebuilt },
  }
  return { request, onRebuilt }
}

afterEach(() => {
  delete (window as unknown as { electronAPI?: unknown }).electronAPI
  vi.restoreAllMocks()
})

describe('maybeRequestConnectionSelfHeal 触发判定', () => {
  it('低于降级阈值不请求（正常退避期偶发掉线）', () => {
    const { request } = installApi()
    maybeRequestConnectionSelfHeal(RECONNECT_DEGRADE_ATTEMPTS - 1)
    expect(request).not.toHaveBeenCalled()
  })

  it('达阈值请求一次，载荷 { reconnectAttempts } 与 IPC 两端字段对齐', () => {
    const { request } = installApi()
    maybeRequestConnectionSelfHeal(RECONNECT_DEGRADE_ATTEMPTS)
    expect(request).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledWith({ reconnectAttempts: RECONNECT_DEGRADE_ATTEMPTS })
  })

  it('超阈值但非阈值整数倍不请求（节拍控制，冷却限频归主进程）', () => {
    const { request } = installApi()
    maybeRequestConnectionSelfHeal(RECONNECT_DEGRADE_ATTEMPTS + 3)
    expect(request).not.toHaveBeenCalled()
  })

  it('阈值整数倍（第 20 次）再请求一轮', () => {
    const { request } = installApi()
    maybeRequestConnectionSelfHeal(RECONNECT_DEGRADE_ATTEMPTS * 2)
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('无 electronAPI（浏览器/dev/移动壳）→ no-op 不炸', () => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    expect(() =>
      maybeRequestConnectionSelfHeal(RECONNECT_DEGRADE_ATTEMPTS),
    ).not.toThrow()
  })

  it('request 拒绝 → 不产生 unhandled rejection（自愈不可用，重连循环兜底）', async () => {
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    installApi(async () => {
      throw new Error('主进程不可用')
    })
    maybeRequestConnectionSelfHeal(RECONNECT_DEGRADE_ATTEMPTS)
    await new Promise((r) => setTimeout(r, 0))
    expect(unhandled).not.toHaveBeenCalled()
    process.off('unhandledRejection', unhandled)
  })
})

describe('subscribeConnectionRebuilt', () => {
  it('有宿主能力 → 透传订阅并返回解订阅函数', () => {
    const { onRebuilt } = installApi()
    const unsub = subscribeConnectionRebuilt(() => {})
    expect(onRebuilt).toHaveBeenCalledTimes(1)
    expect(typeof unsub).toBe('function')
    unsub()
  })

  it('无 electronAPI → 返回空解订阅函数不炸', () => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    expect(() => subscribeConnectionRebuilt(() => {})).not.toThrow()
  })
})
