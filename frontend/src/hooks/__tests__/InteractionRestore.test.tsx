/**
 * InteractionRestore.test.tsx
 *
 * 回归测试：页面刷新 / WS 重连后，待处理交互（choice/conversation）应从后端
 * `/interaction/pending` 恢复到 interactionStore，刷新即丢的 bug 不再复现。
 *
 * 根因：interactionStore 无持久化，WS 推送是 fire-and-forget 不重推；
 * useInteractionHandler 缺少恢复 effect，导致刷新后 pending 交互全部丢失。
 */

// jsdom 不实现 scrollIntoView，部分组件渲染路径需要它
Element.prototype.scrollIntoView = () => {}

import { act, renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

// ---------------------------------------------------------------------------
//  可变 mock：在测试内动态切换 /interaction/pending 的返回内容
//  用 vi.hoisted 保证在 vi.mock 提升后仍引用同一对象
// ---------------------------------------------------------------------------
const pendingResponse = vi.hoisted(() => ({ items: [] as unknown[] }))
const apiGetMock = vi.hoisted(() => vi.fn(async (url: string) => {
  if (url.includes('/interaction/pending')) {
    return { data: { items: pendingResponse.items, total: pendingResponse.items.length } }
  }
  return { data: {} }
}))

vi.mock('@/services/api/client', () => ({
  default: {
    get: apiGetMock,
  },
}))

// globalWS：避免真实 WS 连接，只保留空操作
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn(),
    unsubscribe: vi.fn(),
    sendInteractionResponse: vi.fn().mockResolvedValue(undefined),
  },
}))

// 避免 playNotificationSound 真实播放音频
vi.mock('@/utils/audioNotification', () => ({
  playNotificationSound: vi.fn().mockResolvedValue(undefined),
}))

const MemoryRouterWrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(MemoryRouter, null, children)

// ---------------------------------------------------------------------------
//  构造后端 /interaction/pending 返回的 record（嵌套结构：message_data 承载业务字段）
// ---------------------------------------------------------------------------
function makeRecord(overrides: {
  id?: string
  mode?: string
  session_id?: string
  thread_id?: string
  pipeline_id?: string
  title?: string
}): Record<string, unknown> {
  const id = overrides.id || 'req-restore-1'
  return {
    id,
    session_id: overrides.session_id || 'sess-1',
    type: 'interaction_request',
    status: 'pending',
    message_data: {
      interaction_mode: overrides.mode || 'conversation',
      title: overrides.title || '恢复的对话',
      description: '',
      thread_id: overrides.thread_id || 'thread-1',
      tab_id: 'tab-1',
      user_id: 'user-1',
      agent_id: 'agent-1',
      pipeline_id: overrides.pipeline_id || 'pipe-1',
      viewed_at: null,
    },
  }
}

/** 直接播种一条本地 pending 交互（模拟早前 WS 事件入列/上一轮恢复的残留卡） */
function seedPending(requestId: string, timestamp: string): void {
  useInteractionStore.setState((state) => ({
    pendingInteractions: [
      ...state.pendingInteractions,
      {
        requestId,
        mode: 'choice',
        title: '待结算的审批卡',
        description: '',
        threadId: 'thread-1',
        tabId: '',
        agentId: 'agent-1',
        pipelineId: 'pipe-1',
        timestamp,
        status: 'pending',
        sessionId: 'sess-1',
      },
    ],
  }))
}

describe('pending 轮询对账收卡（真机"审批交互卡残影浮层不自动收"回归）', () => {
  beforeEach(() => {
    pendingResponse.items = []
    apiGetMock.mockClear()
    useInteractionStore.setState({ pendingInteractions: [] })
    useNotificationStore.setState({ notifications: [] })
    usePipelineMessageStore.setState({ pipelines: {} })
  })

  it('他端已应答（服务端列表归零）→ 本地残留 pending 卡自动收起', async () => {
    // 卡片早在轮询前已入列（时间戳取过去），服务端 pending 已归零
    seedPending('req-settled-elsewhere', '2026-10-12T00:00:00.000Z')

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await waitFor(() => {
      expect(
        useInteractionStore.getState().pendingInteractions.some(
          (i) => i.requestId === 'req-settled-elsewhere',
        ),
      ).toBe(false)
    })
  })

  it('对账只收 pending 态：已应答/已进入的卡片不受影响（保留 2s 展示链路）', async () => {
    useInteractionStore.setState({
      pendingInteractions: [
        {
          requestId: 'req-responded',
          mode: 'choice',
          title: '本端已应答',
          description: '',
          threadId: 'thread-1',
          tabId: '',
          agentId: 'agent-1',
          timestamp: '2026-10-12T00:00:00.000Z',
          status: 'responded',
          sessionId: 'sess-1',
        },
      ],
    })

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    // responded 态由既有 2s 自动消失链路负责，对账不越权提前清（状态未被改动）
    expect(
      useInteractionStore.getState().pendingInteractions.some(
        (i) => i.requestId === 'req-responded' && i.status === 'responded',
      ),
    ).toBe(true)
  })

  it('在途豁免：入列时间不早于本轮轮询发起时刻的卡片不误收（WS 事件在途保护）', async () => {
    // 时间戳取未来 = 晚于轮询发起时刻入列的卡（守卫边界）
    seedPending('req-inflight', '2099-01-01T00:00:00.000Z')

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(
      useInteractionStore.getState().pendingInteractions.some(
        (i) => i.requestId === 'req-inflight',
      ),
    ).toBe(true)
  })

  it('服务端仍在列的 pending 卡不收（对账只收消失项）', async () => {
    seedPending('req-still-pending', '2026-10-12T00:00:00.000Z')
    pendingResponse.items = [makeRecord({ id: 'req-still-pending', mode: 'choice' })]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(
      useInteractionStore.getState().pendingInteractions.some(
        (i) => i.requestId === 'req-still-pending',
      ),
    ).toBe(true)
  })
})

describe('交互刷新恢复', () => {
  beforeEach(() => {
    pendingResponse.items = []
    apiGetMock.mockClear()
    // 清空 store（action 引用稳定，setState 重置数据即可）
    useInteractionStore.setState({ pendingInteractions: [] })
    useNotificationStore.setState({ notifications: [] })
    usePipelineMessageStore.setState({ pipelines: {} })
  })

  it('conversation 模式 record 应恢复到 interactionStore', async () => {
    pendingResponse.items = [makeRecord({ id: 'req-conv', mode: 'conversation' })]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    // 先确认恢复逻辑确实调用了 /interaction/pending（排除假阳性）
    await waitFor(() => {
      expect(
        apiGetMock.mock.calls.some((c) => String(c[0]).includes('/interaction/pending')),
      ).toBe(true)
    })

    await waitFor(() => {
      expect(
        useInteractionStore.getState().pendingInteractions.some((i) => i.requestId === 'req-conv'),
      ).toBe(true)
    })

    const restored = useInteractionStore.getState().pendingInteractions.find(
      (i) => i.requestId === 'req-conv',
    )
    expect(restored?.mode).toBe('conversation')
    // 关键：pipelineId 必须从 message_data 正确映射（跳转标识符）
    expect(restored?.pipelineId).toBe('pipe-1')
    expect(restored?.threadId).toBe('thread-1')
  })

  it('choice 模式 record 应恢复到 interactionStore', async () => {
    pendingResponse.items = [
      makeRecord({ id: 'req-choice', mode: 'choice', title: '选择' }),
    ]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await waitFor(() => {
      expect(
        useInteractionStore.getState().pendingInteractions.some(
          (i) => i.requestId === 'req-choice',
        ),
      ).toBe(true)
    })
  })

  it('notification 模式 record 不应进入 interactionStore（分流到通知中心）', async () => {
    pendingResponse.items = [makeRecord({ id: 'req-notif', mode: 'notification' })]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    // 给恢复逻辑足够时间执行（notification 不写 store，无法用 waitFor 断言存在）
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })

    expect(
      useInteractionStore.getState().pendingInteractions.some(
        (i) => i.requestId === 'req-notif',
      ),
    ).toBe(false)
  })

  it('notification 模式通知带来源标签：优先管道元数据 agentName', async () => {
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: 'pipe-1',
      sessionId: 'sess-1',
      level: 2,
      tabId: 'tab-1',
      agentName: '代码执行员',
      status: 'running',
      parentId: null,
      unreadCount: 0,
    })
    pendingResponse.items = [makeRecord({ id: 'req-notif-src', mode: 'notification' })]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '恢复的对话')
    expect(notif?.sourceLabel).toBe('代码执行员')
  })

  it('notification 模式通知来源标签：无管道元数据时回退 agentId', async () => {
    pendingResponse.items = [makeRecord({ id: 'req-notif-fb', mode: 'notification' })]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '恢复的对话')
    expect(notif?.sourceLabel).toBe('agent-1')
  })

  it('request_id 缺失时应用顶层 id 兜底（normalizeRecord 映射正确）', async () => {
    pendingResponse.items = [
      {
        id: 'top-level-id',
        session_id: 'sess-1',
        type: 'interaction_request',
        status: 'pending',
        message_data: {
          interaction_mode: 'conversation',
          title: '顶层 id 兜底',
          thread_id: 'thread-x',
          pipeline_id: 'pipe-x',
        },
      },
    ]

    renderHook(() => useInteractionHandler('sess-1'), { wrapper: MemoryRouterWrapper })

    await waitFor(() => {
      expect(
        useInteractionStore.getState().pendingInteractions.some(
          (i) => i.requestId === 'top-level-id',
        ),
      ).toBe(true)
    })
  })
})
