/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * 交互响应路由键测试（2026-08-22 裁决）
 *
 * 反模式收口：respondChoice/respondConversation 在交互自带坐标缺失时兜底
 * 全局 activeSessionId——用户在等待期间切换会话，审批被发到错误 thread 通道。
 *
 * 新规则：路由键只取交互自身坐标（sessionId → threadId），换不到即中止
 * （fail-closed，交互保持 pending 可见可重试），绝不落"当前活跃会话"。
 */
import { act, renderHook } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// globalWS：记录 sendInteractionResponse 调用，不建真实连接
const sendSpy = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn(),
    unsubscribe: vi.fn(),
    sendInteractionResponse: sendSpy,
  },
}))

vi.mock('@/utils/audioNotification', () => ({
  playNotificationSound: vi.fn().mockResolvedValue(undefined),
}))

// 恢复接口返回空列表；sessionStore mock 出"别的活跃会话"以证明不被兜底
vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(async () => ({ data: { items: [], total: 0 } })),
  },
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: {
    getState: () => ({ activeSessionId: 'active-other-session' }),
  },
}))

import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import type { PendingInteraction } from '@/stores/interactionStore'

const MemoryRouterWrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(MemoryRouter, null, children)

function enteredInteraction(overrides: Partial<PendingInteraction>): PendingInteraction {
  return {
    requestId: 'req-routing-1',
    mode: 'choice',
    title: '审批',
    description: '',
    threadId: 'thread-X',
    tabId: 'tab-1',
    agentId: 'agent-X',
    timestamp: '2026-08-22T00:00:00.000Z',
    status: 'entered',
    ...overrides,
  }
}

beforeEach(() => {
  useInteractionStore.setState({ pendingInteractions: [] })
  sendSpy.mockClear()
})

describe('交互响应路由键（只取交互自身坐标）', () => {
  it('respondChoice 用交互自带 sessionId（不受活跃会话影响）', async () => {
    useInteractionStore.setState({
      pendingInteractions: [enteredInteraction({ requestId: 'req-1', sessionId: 'sess-own' })],
    })
    const { result } = renderHook(() => useInteractionHandler(), { wrapper: MemoryRouterWrapper })
    await act(async () => {
      await result.current!.respondChoice('req-1', 'option-a')
    })
    expect(sendSpy).toHaveBeenCalledTimes(1)
    expect(sendSpy.mock.calls[0][0]).toBe('sess-own')
    expect(sendSpy.mock.calls[0][0]).not.toBe('active-other-session')
  })

  it('respondChoice 无 sessionId 时回落交互自身 threadId，绝不落活跃会话', async () => {
    useInteractionStore.setState({
      pendingInteractions: [enteredInteraction({ requestId: 'req-2', sessionId: undefined })],
    })
    const { result } = renderHook(() => useInteractionHandler(), { wrapper: MemoryRouterWrapper })
    await act(async () => {
      await result.current!.respondChoice('req-2', 'option-a')
    })
    expect(sendSpy).toHaveBeenCalledTimes(1)
    expect(sendSpy.mock.calls[0][0]).toBe('thread-X')
    expect(sendSpy.mock.calls[0][0]).not.toBe('active-other-session')
  })

  it('respondConversation 同规则', async () => {
    useInteractionStore.setState({
      pendingInteractions: [enteredInteraction({ requestId: 'req-3', sessionId: undefined })],
    })
    const { result } = renderHook(() => useInteractionHandler(), { wrapper: MemoryRouterWrapper })
    await act(async () => {
      await result.current!.respondConversation('req-3', 'ok')
    })
    expect(sendSpy).toHaveBeenCalledTimes(1)
    expect(sendSpy.mock.calls[0][0]).toBe('thread-X')
  })

  it('交互无任何自身坐标 → 中止发送（fail-closed，不猜活跃会话）', async () => {
    useInteractionStore.setState({
      pendingInteractions: [
        enteredInteraction({ requestId: 'req-4', sessionId: undefined, threadId: '' }),
      ],
    })
    const { result } = renderHook(() => useInteractionHandler(), { wrapper: MemoryRouterWrapper })
    await act(async () => {
      await result.current!.respondChoice('req-4', 'option-a')
    })
    expect(sendSpy).not.toHaveBeenCalled()
  })
})