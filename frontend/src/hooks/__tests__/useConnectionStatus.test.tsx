/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * useConnectionStatus 首连状态映射测试（回归：2026-08-20 用户真实反馈）。
 *
 * Bug 现象：每次刷新页面后，"内核连接已断开，正在尝试恢复…"横幅显示
 * 5-10s 才消失——用户感知"要等很久才能连上后端"。
 *
 * 根因：layoutModeStore.connectionStatus 初始 state='disconnected'，且
 * useConnectionStatus 在「WS 尚未发起首次连接」（token 恢复/JS 加载期）
 * 时把 globalWS.status='disconnected'（从未连接≠断开）也映射成
 * 'disconnected'——页面刷新后横幅立即出现"已断开"；连上后 AlertBanner
 * 的 resolveHoldMs=4s 又让横幅多挂 4s。
 *
 * 契约（修复后）：从未发起过连接（hasAttemptedConnect=false）或正在
 * connecting 时，状态映射为 'connecting'（不出断开横幅）；只有真正
 * 连接过之后的断开才是 'disconnected'。
 */
import { renderHook } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'

const wsState = {
  status: 'disconnected' as string,
  hasAttemptedConnect: false,
  handlers: new Map<string, Set<(d: unknown) => void>>(),
}

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    get status() {
      return wsState.status
    },
    get hasAttemptedConnect() {
      return wsState.hasAttemptedConnect
    },
    subscribe: vi.fn((event: string, handler: (d: unknown) => void) => {
      if (!wsState.handlers.has(event)) wsState.handlers.set(event, new Set())
      wsState.handlers.get(event)!.add(handler)
    }),
    unsubscribe: vi.fn((event: string, handler: (d: unknown) => void) => {
      wsState.handlers.get(event)?.delete(handler)
    }),
  },
}))

import { useConnectionStatus } from '../useConnectionStatus'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

describe('useConnectionStatus 首连状态映射', () => {
  beforeEach(() => {
    wsState.status = 'disconnected'
    wsState.hasAttemptedConnect = false
    wsState.handlers.clear()
    useLayoutModeStore.setState({
      connectionStatus: {
        state: 'connecting',
        latencyMs: null,
        reconnectAttempt: 0,
        lastConnectedAt: null,
        queuedMessages: 0,
      },
    })
  })

  it('WS 从未发起连接时（刷新后 token 恢复期）：状态保持 connecting，不误报 disconnected', () => {
    // globalWS.status='disconnected' 是初始值（从未 connect 过）
    renderHook(() => useConnectionStatus())

    expect(useLayoutModeStore.getState().connectionStatus.state).toBe('connecting')
  })

  it('connect 已发起（connecting 中）：状态为 connecting', () => {
    wsState.status = 'connecting'
    wsState.hasAttemptedConnect = true
    renderHook(() => useConnectionStatus())

    expect(useLayoutModeStore.getState().connectionStatus.state).toBe('connecting')
  })

  it('连接成功：状态为 connected', () => {
    wsState.status = 'connected'
    wsState.hasAttemptedConnect = true
    renderHook(() => useConnectionStatus())

    expect(useLayoutModeStore.getState().connectionStatus.state).toBe('connected')
  })

  it('曾经连过之后的断开：状态为 disconnected（真断开仍要出横幅）', () => {
    wsState.status = 'disconnected'
    wsState.hasAttemptedConnect = true
    renderHook(() => useConnectionStatus())

    expect(useLayoutModeStore.getState().connectionStatus.state).toBe('disconnected')
  })

  it('_status 事件：connected 通知同步到 store', () => {
    const { result } = renderHook(() => useConnectionStatus())
    void result
    const handler = [...(wsState.handlers.get('_status') ?? [])][0]
    expect(handler).toBeDefined()
    handler({ status: 'connected' })
    expect(useLayoutModeStore.getState().connectionStatus.state).toBe('connected')
    expect(useLayoutModeStore.getState().connectionStatus.lastConnectedAt).not.toBeNull()
  })
})
