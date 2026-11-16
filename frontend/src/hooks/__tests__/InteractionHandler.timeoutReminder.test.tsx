/** @feature: FP-T12 前端适配（P-20/U-2 审批超时前提醒） | @ci: frontend-test */
/**
 * 超时前提醒订阅腿（P-20/U-2）：interaction_timeout_reminder 帧消费。
 *
 * 后端每请求提醒一次（35b920af9 修复 payload.thread_id 路由后可达）。
 * 锁三面：
 * - 持卡端：置提醒态（卡片切「即将超时」高亮）+ 高优通知一次
 *   （稳定 id，重放不重复）；
 * - 非持卡端 / 缺 request_id：静默（APPROVAL_TAKEN 同纪律——提醒只面向
 *   正在等待的端，不无故打扰）；
 * - 负载缺 remaining_seconds → 降级文案（不伪造剩余时间）。
 *
 * 各用例独立 request_id：通知 store 对「用户清除过的 id」不回弹（全局
 * 记忆），复用 id 会跨用例互吞。先挂载后播种：挂载即拉取的恢复面会把
 * 「服务端清单外且早于本轮起点」的本地卡摘除，播种晚于该轮起点才稳。
 */
import { act, renderHook } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore, type PendingInteraction } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'

const pendingResponse = vi.hoisted(() => ({ items: [] as unknown[] }))
const apiGetMock = vi.hoisted(() => vi.fn(async (url: string) => {
  if (String(url).includes('/interaction/pending')) {
    return { data: { items: pendingResponse.items, total: pendingResponse.items.length } }
  }
  return { data: { success: true, content: '文件内容甲' } }
}))

const ws = vi.hoisted(() => {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  return {
    handlers,
    subscribe: vi.fn((topic: string, fn: (data: unknown) => void) => {
      if (!handlers.has(topic)) handlers.set(topic, new Set())
      handlers.get(topic)!.add(fn)
    }),
    unsubscribe: vi.fn((topic: string, fn: (data: unknown) => void) => {
      handlers.get(topic)?.delete(fn)
    }),
    sendInteractionResponse: vi.fn().mockResolvedValue(undefined),
    emit: (topic: string, data: unknown) => {
      handlers.get(topic)?.forEach((fn) => fn(data))
    },
  }
})

vi.mock('@/services/api/client', () => ({ default: { get: apiGetMock } }))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({ globalWS: ws }))
vi.mock('@/utils/audioNotification', () => ({ playNotificationSound: vi.fn(async () => false) }))
vi.mock('@/utils/systemNotification', () => ({ showSystemNotification: vi.fn(async () => true) }))
vi.mock('@/services/pipelineNavigator', () => ({ navigateToPipeline: vi.fn().mockResolvedValue(undefined) }))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({ readAgents: () => [] }))

const emitReminder = (data: Record<string, unknown>) =>
  ws.emit('interaction_timeout_reminder', {
    type: 'interaction_timeout_reminder',
    data,
    sequence: 1,
  })

/** useInteractionHandler 内部用 useNavigate → 须包 Router（Branches 夹具同款） */
const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(MemoryRouter, null, children)

/** 挂载后播种一张 pending 审批卡（晚于挂载恢复面的本轮起点，不被摘除） */
function mountAndSeed(requestId: string) {
  const view = renderHook(() => useInteractionHandler(), { wrapper: Wrapper })
  const interaction: PendingInteraction = {
    requestId,
    mode: 'choice',
    title: '安全审批: bash_execute',
    description: '',
    threadId: 'th-1',
    tabId: 'tb-1',
    agentId: 'ag-1',
    timeoutSeconds: 600,
    createdAt: new Date().toISOString(),
    timestamp: new Date().toISOString(),
    status: 'pending',
  }
  act(() => {
    useInteractionStore.getState().addInteraction(interaction)
  })
  return view
}

describe('useInteractionHandler: 超时前提醒（P-20）', () => {
  beforeEach(() => {
    useInteractionStore.setState({ pendingInteractions: [], globalOpenRequestId: null })
    useNotificationStore.getState().clearAll()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('持卡端：置提醒态 + 高优通知一次（含剩余秒数）', () => {
    const { unmount } = mountAndSeed('req-held')
    act(() => {
      emitReminder({ request_id: 'req-held', remaining_seconds: 30, title: '安全审批', mode: 'choice' })
    })

    expect(useInteractionStore.getState().pendingInteractions[0]?.timeoutReminded).toBe(true)
    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.id === 'interaction-timeout-reminder-req-held')
    expect(notif).toBeDefined()
    expect(notif?.title).toBe('审批即将超时')
    expect(notif?.message).toContain('剩余 30 秒')
    expect(notif?.message).toContain('自动按拒绝裁决')
    expect(notif?.priority).toBe('high')
    unmount()
  })

  it('同帧重放（稳定 id）→ 通知不重复，提醒态幂等', () => {
    const { unmount } = mountAndSeed('req-replay')
    const frame = { request_id: 'req-replay', remaining_seconds: 30 }
    act(() => {
      emitReminder(frame)
      emitReminder(frame)
    })

    expect(
      useNotificationStore
        .getState()
        .notifications.filter((n) => n.id === 'interaction-timeout-reminder-req-replay'),
    ).toHaveLength(1)
    expect(useInteractionStore.getState().pendingInteractions[0]?.timeoutReminded).toBe(true)
    unmount()
  })

  it('非持卡端（本页无该 pending 卡）→ 静默：不通知、不置态', () => {
    const { unmount } = mountAndSeed('req-other')
    act(() => {
      emitReminder({ request_id: 'req-unknown', remaining_seconds: 30 })
    })

    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(useInteractionStore.getState().pendingInteractions[0]?.timeoutReminded).toBeUndefined()
    unmount()
  })

  it('负载缺 remaining_seconds → 降级文案（无剩余时间不伪造）', () => {
    const { unmount } = mountAndSeed('req-degraded')
    act(() => {
      emitReminder({ request_id: 'req-degraded' })
    })

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.id === 'interaction-timeout-reminder-req-degraded')
    expect(notif?.message).toBe('超时将自动按拒绝裁决，请尽快处理。')
    unmount()
  })

  it('缺 request_id → 整帧丢弃', () => {
    const { unmount } = mountAndSeed('req-noop')
    act(() => {
      emitReminder({ remaining_seconds: 30 })
    })

    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(useInteractionStore.getState().pendingInteractions[0]?.timeoutReminded).toBeUndefined()
    unmount()
  })
})
