/** @feature X30-F1 P-144 | @ci: frontend-test */
/**
 * useInteractionHandler notification 模式 priority 白名单校验（P-144）：
 * human 插件/LLM 可能把 priority 发成白名单外任意值（'urgent'/'Critical'/
 * 数字），修复前直传 store（渲染层 PRIORITY_STYLES[priority]=undefined，
 * 样式兜底缺失即崩）。覆盖：
 * - 非法字符串/大小写畸形 → 回退本入口默认档 high，不崩溃
 * - 合法四档（含 critical）原样透传（不过度归一化）
 * - priority 缺失 → 默认 high（既有语义，不告警）
 * - 非法值告警按值去重只 warn 一次（轮询重放不刷屏），换值再告警
 * - 音频失败兜底通知（choice 模式第二个 sink）同走白名单
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useNotificationStore } from '@/stores/notificationStore'
import { playNotificationSound } from '@/utils/audioNotification'
import { loggers } from '@/utils/logger'

// vi.mock/vi.hoisted 由 vitest transform 提升到 import 之前，工厂体内对
// vi.hoisted 常量的引用是惰性求值，源码位置在 import 之后不影响行为
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

const mockNavigateToPipeline = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))
const mockAgents = vi.hoisted(() => ({ list: [] as Array<{ id: string; configId?: string; name: string }> }))

vi.mock('@/services/api/client', () => ({ default: { get: apiGetMock } }))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({ globalWS: ws }))
vi.mock('@/utils/audioNotification', () => ({
  // 默认 resolved：拒绝路径的用例各自 mockRejectedValue 覆写（同
  // InteractionHandlerBranches 惯例，避免 unhandled rejection）
  playNotificationSound: vi.fn(async () => false),
}))
vi.mock('@/utils/systemNotification', () => ({
  showSystemNotification: vi.fn(async () => true),
}))
vi.mock('@/services/pipelineNavigator', () => ({ navigateToPipeline: mockNavigateToPipeline }))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({ readAgents: () => mockAgents.list }))

const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(MemoryRouter, null, children)

/** 挂载 hook（真订阅），返回其出口；unmount 由测试自行调用 */
async function mountHandler() {
  const view = renderHook(() => useInteractionHandler(undefined), { wrapper: Wrapper })
  await waitFor(() => expect(ws.handlers.has('interaction_request')).toBe(true))
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  ws.handlers.clear()
  pendingResponse.items = []
  mockAgents.list = []
  useNotificationStore.setState(useNotificationStore.getInitialState(), true)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('notification 模式 priority 白名单校验（P-144）', () => {
  /** 推一条带指定 priority 的 notification 模式交互，返回入列条目 */
  async function pushWithPriority(priority: unknown, requestId = 'req-prio') {
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: requestId,
      interaction_mode: 'notification',
      title: '进度',
      session_id: 's',
      thread_id: 't',
      pipeline_id: 'p',
      ...(priority === undefined ? {} : { priority }),
    })
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications).toHaveLength(1),
    )
    const stored = useNotificationStore.getState().notifications[0]
    view.unmount()
    return stored
  }

  it('非法字符串（urgent）→ 回退默认档 high，不崩溃', async () => {
    const stored = await pushWithPriority('urgent')
    expect(stored.id).toBe('req-prio')
    expect(stored.priority).toBe('high')
  })

  it('大小写畸形（Critical）不在白名单 → 回退 high', async () => {
    expect((await pushWithPriority('Critical')).priority).toBe('high')
  })

  it('数字型非法值 → 回退 high', async () => {
    expect((await pushWithPriority(3)).priority).toBe('high')
  })

  it('合法 critical 原样透传（白名单含四档，不过度归一化）', async () => {
    expect((await pushWithPriority('critical')).priority).toBe('critical')
  })

  it('合法 normal 原样透传', async () => {
    expect((await pushWithPriority('normal')).priority).toBe('normal')
  })

  it('priority 缺失 → 默认 high（既有语义不变，不告警）', async () => {
    const warnSpy = vi.spyOn(loggers.notification, 'warn')
    expect((await pushWithPriority(undefined)).priority).toBe('high')
    expect(warnSpy).not.toHaveBeenCalled()
  })
})

describe('非法 priority 告警去重（warn 一次）', () => {
  it('同值重放只 warn 一次；换值再 warn 一次', async () => {
    // 去重账本是模块级 Set（跨用例残留），故用本文件唯一的新值保证断言自含
    const warnSpy = vi.spyOn(loggers.notification, 'warn')
    const view = await mountHandler()
    const payload = {
      request_id: 'req-warn',
      interaction_mode: 'notification',
      title: '进度',
      session_id: 's',
      thread_id: 't',
      pipeline_id: 'p',
      priority: 'urgent-dup-alpha',
    }
    ws.emit('interaction_request', payload)
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications).toHaveLength(1),
    )
    // 同 request_id 重放（pending 兜底轮询形态）：幂等闸更新条目，warn 不重复
    ws.emit('interaction_request', payload)
    await act(async () => {})
    expect(warnSpy).toHaveBeenCalledTimes(1)

    ws.emit('interaction_request', { ...payload, request_id: 'req-warn2', priority: 'P0-dup-beta' })
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications).toHaveLength(2),
    )
    expect(warnSpy).toHaveBeenCalledTimes(2)
    view.unmount()
  })
})

describe('音频失败兜底通知的 priority 同走白名单', () => {
  it('choice 模式 priority=urgent + 音频失败 → 兜底通知回退 high', async () => {
    vi.mocked(playNotificationSound).mockRejectedValue(new Error('no audio'))
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-audio',
      interaction_mode: 'choice',
      title: '审批',
      description: '请确认',
      session_id: 's',
      thread_id: 't',
      pipeline_id: 'p',
      priority: 'urgent',
    })
    await waitFor(() =>
      expect(
        useNotificationStore.getState().notifications.some((n) => n.message === '请确认'),
      ).toBe(true),
    )
    const fallback = useNotificationStore
      .getState()
      .notifications.find((n) => n.message === '请确认')
    expect(fallback?.priority).toBe('high')
    view.unmount()
  })

  it('choice 模式 priority 合法（high）+ 音频失败 → 兜底通知保持 high', async () => {
    vi.mocked(playNotificationSound).mockRejectedValue(new Error('no audio'))
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-audio-ok',
      interaction_mode: 'choice',
      title: '审批',
      description: '请确认乙',
      session_id: 's',
      thread_id: 't',
      pipeline_id: 'p',
      priority: 'high',
    })
    await waitFor(() =>
      expect(
        useNotificationStore.getState().notifications.some((n) => n.message === '请确认乙'),
      ).toBe(true),
    )
    const fallback = useNotificationStore
      .getState()
      .notifications.find((n) => n.message === '请确认乙')
    expect(fallback?.priority).toBe('high')
    view.unmount()
  })
})
