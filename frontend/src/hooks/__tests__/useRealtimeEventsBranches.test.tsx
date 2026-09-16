/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * useRealtimeEvents 分支补测：把 hook 订阅的每条事件 handler 的正/负/缺省分支走全——
 * 重连补漏（防抖、无会话、无主管道、backfill 失败通知）、task_status_update
 * （缓存命中/未命中、phase/error 增量）、task_deleted、pending_inputs_changed
 * （returned 回填用户消息 / 常规同步）、compression_failed、kicked、
 * visibility 回前台重连链路（可见性/已连接/被踢/token 三种结局）。
 *
 * GlobalWebSocket 与 tokenLifecycle 是外部依赖（网络层），用可控 stub 替换并
 * 记录订阅/退订调用；数据面用全局 queryClient + 真实 store 断言可观察副作用。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WS_LOCAL_EVENTS, WS_SERVER_EVENTS } from '@/constants/websocket'

const listeners: Record<string, Set<(...args: unknown[]) => void>> = {}
const wsState = {
  status: 'connected' as string,
  kicked: false,
}
const connectMock = vi.fn()
const subscribeMock = vi.fn((event: string, cb: (...args: never[]) => void) => {
  if (!listeners[event]) listeners[event] = new Set()
  listeners[event].add(cb)
})
const unsubscribeMock = vi.fn((event: string, cb: (...args: never[]) => void) => {
  listeners[event]?.delete(cb)
})
const wasKickedMock = vi.fn(() => wsState.kicked)

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, cb: (...args: never[]) => void) => subscribeMock(event, cb),
    unsubscribe: (event: string, cb: (...args: never[]) => void) => unsubscribeMock(event, cb),
    connect: (...args: unknown[]) => connectMock(...args),
    wasKickedByReplacement: () => wasKickedMock(),
    get status() {
      return wsState.status
    },
  },
}))

const ensureFreshTokenMock = vi.fn()
vi.mock('@/services/auth/tokenLifecycle', () => ({
  ensureFreshToken: () => ensureFreshTokenMock(),
}))

import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'

function emit(event: string, payload: unknown) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of [...cbs]) cb(payload)
}

/** 订阅是否在册（判断卸载是否退订干净） */
function subscribedCount(event: string): number {
  return listeners[event]?.size ?? 0
}

beforeEach(() => {
  for (const key of Object.keys(listeners)) delete listeners[key]
  vi.clearAllMocks()
  wsState.status = 'connected'
  wsState.kicked = false
  queryClient.clear()
  useNotificationStore.getState().clearAll()
  useChatInputStore.setState({ pendingInsert: null })
  usePendingInputStore.setState({ byPipeline: {}, editingId: {} })
  useSessionStore.setState({ activeSessionId: null })
  useLongTermTaskStore.setState({ activeTaskId: null })
  useLayoutModeStore.setState({ workspaceDataVersion: 0 })
})

afterEach(() => {
  vi.clearAllMocks()
})

/** 订阅面全集（本地 + 服务器事件），订阅/退订断言共用 */
const ALL_REALTIME_EVENTS = [
  WS_LOCAL_EVENTS.RECONNECTED,
  WS_SERVER_EVENTS.TASK_STATUS_UPDATE,
  WS_SERVER_EVENTS.TASK_STATUS_CHANGED,
  WS_SERVER_EVENTS.TASK_DELETED,
  WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT,
  WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED,
  WS_SERVER_EVENTS.COMPRESSION_FAILED,
  WS_LOCAL_EVENTS.KICKED_BY_REPLACEMENT,
]

/** 重连补拉族用例共用：播种活跃会话缓存 + 桩定 loadPipelineMessages */
/** 通知断言族共用：播种会话 + 注入带自定义应答的 loadPipelineMessages */
function seedSessionWithLoadResponse(
  sessions: Array<Record<string, unknown>>,
  activeSessionId: string,
  resolved: Record<string, unknown>,
) {
  useSessionStore.setState({ activeSessionId })
  queryClient.setQueryData(queryKeys.sessions, sessions as never)
  usePipelineMessageStore.setState({
    loadPipelineMessages: vi.fn().mockResolvedValue(resolved),
  } as never)
}

function seedSessionWithLoadSpy(
  sessions: Array<Record<string, unknown>>,
  activeSessionId: string,
) {
  useSessionStore.setState({ activeSessionId })
  queryClient.setQueryData(queryKeys.sessions, sessions as never)
  const loadSpy = vi.fn().mockResolvedValue({ ok: true })
  usePipelineMessageStore.setState({ loadPipelineMessages: loadSpy } as never)
  return loadSpy
}

/** task 事件族共用：播种任务缓存 + 记录版本号 + 挂载 + 发事件，返回断言上下文 */
async function seedTasksMountAndEmit(
  tasks: Array<Record<string, unknown>>,
  eventType: string,
  payload: Record<string, unknown>,
) {
  queryClient.setQueryData(queryKeys.longTermTasks, tasks as never)
  const before = useLayoutModeStore.getState().workspaceDataVersion
  const { unmount } = renderHook(() => useRealtimeEvents())
  act(() => emit(eventType, payload))
  return { before, unmount }
}

/** task 事件族共用：播种任务缓存 + 记录版本号 + 挂载（emit 留在用例内） */
function seedTasksAndMount(tasks: Array<Record<string, unknown>>) {
  queryClient.setQueryData(queryKeys.longTermTasks, tasks as never)
  const before = useLayoutModeStore.getState().workspaceDataVersion
  const { unmount } = renderHook(() => useRealtimeEvents())
  return { before, unmount }
}

describe('useRealtimeEvents — 订阅与退订覆盖面', () => {
  it('挂载时订阅全部 8 条事件（含本地与服务器事件）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    for (const evt of ALL_REALTIME_EVENTS) {
      expect(subscribedCount(evt)).toBeGreaterThan(0)
    }
    unmount()
  })

  it('卸载后退订全部订阅并移除 visibilitychange 监听（无残留 handler）', () => {
    const removeSpy = vi.spyOn(document, 'removeEventListener')
    const { unmount } = renderHook(() => useRealtimeEvents())
    unmount()
    for (const evt of ALL_REALTIME_EVENTS) {
      expect(subscribedCount(evt)).toBe(0)
    }
    expect(removeSpy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    removeSpy.mockRestore()
  })
})

describe('useRealtimeEvents — 重连补漏 handleWsReconnect', () => {
  it('无活跃会话时只失效 query 缓存，不触发消息补拉', () => {
    useSessionStore.setState({ activeSessionId: null })
    const loadSpy = vi.spyOn(usePipelineMessageStore.getState(), 'loadPipelineMessages')
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')
    const { unmount } = renderHook(() => useRealtimeEvents())

    act(() => emit(WS_LOCAL_EVENTS.RECONNECTED, {}))

    expect(invalidateSpy).toHaveBeenCalled()
    expect(loadSpy).not.toHaveBeenCalled()
    unmount()
    loadSpy.mockRestore()
    invalidateSpy.mockRestore()
  })

  it('有活跃会话与主管道时以 backfill 模式补拉该主管道', async () => {
    const loadSpy = seedSessionWithLoadSpy(
      [{ id: 'sess-1', activePipelineId: 'pipe-main', pipelineIds: ['pipe-main', 'pipe-sub'] }],
      'sess-1',
    )

    const { unmount } = renderHook(() => useRealtimeEvents())
    await act(async () => {
      emit(WS_LOCAL_EVENTS.RECONNECTED, {})
    })

    expect(loadSpy).toHaveBeenCalledTimes(1)
    expect(loadSpy.mock.calls[0][0]).toBe('pipe-main')
    expect(loadSpy.mock.calls[0][1]).toMatchObject({
      threadId: 'sess-1',
      mode: 'backfill',
      skipStreamingCheck: true,
    })
    unmount()
  })

  it('会话存在但解析不出主管道（多管道且无 activePipelineId）时不补拉', () => {
    const loadSpy = seedSessionWithLoadSpy(
      [{ id: 'sess-2', pipelineIds: ['p1', 'p2'] }],
      'sess-2',
    )

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_LOCAL_EVENTS.RECONNECTED, {}))

    expect(loadSpy).not.toHaveBeenCalled()
    unmount()
  })

  it('1 秒内重复重连事件只补拉一次（防抖）', async () => {
    const loadSpy = seedSessionWithLoadSpy(
      [{ id: 'sess-1', activePipelineId: 'pipe-main' }],
      'sess-1',
    )

    const { unmount } = renderHook(() => useRealtimeEvents())
    await act(async () => {
      emit(WS_LOCAL_EVENTS.RECONNECTED, {})
      emit(WS_LOCAL_EVENTS.RECONNECTED, {})
    })

    expect(loadSpy).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('activeSessionId 指向的会话不在缓存中时不补拉（session 查找未命中）', () => {
    const loadSpy = seedSessionWithLoadSpy(
      [{ id: 'sess-other', activePipelineId: 'p-x' }],
      'sess-missing',
    )

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_LOCAL_EVENTS.RECONNECTED, {}))

    expect(loadSpy).not.toHaveBeenCalled()
    unmount()
  })

  it('补拉返回 ok=false 时发高优错误通知（含标题与分类）', async () => {
    seedSessionWithLoadResponse([{ id: 'sess-1', activePipelineId: 'pipe-main' }], 'sess-1', { ok: false, error: new Error('boom') })

    const { unmount } = renderHook(() => useRealtimeEvents())
    await act(async () => {
      emit(WS_LOCAL_EVENTS.RECONNECTED, {})
    })
    // 等 then 回调落定
    await act(async () => {})

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '消息同步失败')
    expect(notif).toBeDefined()
    expect(notif?.priority).toBe('high')
    expect(notif?.category).toBe('error')
    unmount()
  })

  it('补拉成功（ok=true）不产生错误通知', async () => {
    seedSessionWithLoadResponse([{ id: 'sess-1', activePipelineId: 'pipe-main' }], 'sess-1', { ok: true })

    const { unmount } = renderHook(() => useRealtimeEvents())
    await act(async () => {
      emit(WS_LOCAL_EVENTS.RECONNECTED, {})
    })
    await act(async () => {})

    expect(
      useNotificationStore.getState().notifications.filter((n) => n.title === '消息同步失败'),
    ).toEqual([])
    unmount()
  })
})

describe('useRealtimeEvents — task_status_update', () => {
  it('任务已在缓存中：增量写 status/currentPhase/error，并 bump 工作区版本', () => {
    const { before, unmount } = seedTasksAndMount([{ id: 't1', status: 'running' }, { id: 't2', status: 'running' }])
    act(() =>
      emit(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, {
        task_id: 't1',
        new_status: 'blocked',
        current_phase: 'planning',
        error: '上游失败',
      }),
    )

    const tasks = queryClient.getQueryData<Array<Record<string, unknown>>>(queryKeys.longTermTasks)!
    expect(tasks.find((t) => t.id === 't1')).toMatchObject({
      status: 'blocked',
      currentPhase: 'planning',
      error: '上游失败',
    })
    expect(tasks.find((t) => t.id === 't2')).toMatchObject({ status: 'running' })
    expect(useLayoutModeStore.getState().workspaceDataVersion).toBe(before + 1)
    unmount()
  })

  it('事件体嵌在 data 包装层时同样被解包处理（taskId 驼峰变体）', () => {
    queryClient.setQueryData(queryKeys.longTermTasks, [{ id: 't9', status: 'running' }] as never)

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, {
        data: { taskId: 't9', new_status: 'stopped' },
      }),
    )

    const tasks = queryClient.getQueryData<Array<Record<string, unknown>>>(queryKeys.longTermTasks)!
    expect(tasks[0]).toMatchObject({ id: 't9', status: 'stopped' })
    unmount()
  })

  it('任务不在缓存中：走失效重拉路径（invalidateQueries 命中 longTermTasks key）', () => {
    queryClient.setQueryData(queryKeys.longTermTasks, [{ id: 'other', status: 'running' }] as never)
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, {
        task_id: 'unknown',
        new_status: 'running',
      }),
    )

    expect(
      invalidateSpy.mock.calls.some((c) => JSON.stringify(c[0]).includes('long-term-tasks')),
    ).toBe(true)
    invalidateSpy.mockRestore()
    unmount()
  })

  it('缺 task_id 或 new_status 时不改缓存，但仍 bump 工作区版本', () => {
    queryClient.setQueryData(queryKeys.longTermTasks, [{ id: 't1', status: 'running' }] as never)
    const before = useLayoutModeStore.getState().workspaceDataVersion

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, { task_id: 't1' }))
    act(() => emit(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, { new_status: 'blocked' }))

    const tasks = queryClient.getQueryData<Array<Record<string, unknown>>>(queryKeys.longTermTasks)!
    expect(tasks[0]).toMatchObject({ status: 'running' })
    expect(useLayoutModeStore.getState().workspaceDataVersion).toBe(before + 2)
    unmount()
  })

  it('phase/error 缺省时不写入这两个键（只更新 status）', () => {
    queryClient.setQueryData(queryKeys.longTermTasks, [{ id: 't1', status: 'running' }] as never)

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, { task_id: 't1', new_status: 'completed' }),
    )

    const task = queryClient.getQueryData<Array<Record<string, unknown>>>(queryKeys.longTermTasks)![0]
    expect(task).toMatchObject({ status: 'completed' })
    expect('currentPhase' in task).toBe(false)
    expect('error' in task).toBe(false)
    unmount()
  })
})

describe('useRealtimeEvents — task_deleted 与 task_status_changed', () => {
  it('task_deleted 移除缓存任务并 bump 版本号', () => {
    const { before, unmount } = seedTasksAndMount([{ id: 't1', status: 'running' }, { id: 't2', status: 'running' }])
    act(() => emit(WS_SERVER_EVENTS.TASK_DELETED, { task_id: 't1' }))

    const tasks = queryClient.getQueryData<Array<Record<string, unknown>>>(queryKeys.longTermTasks)!
    expect(tasks.map((t) => t.id)).toEqual(['t2'])
    expect(useLayoutModeStore.getState().workspaceDataVersion).toBe(before + 1)
    unmount()
  })

  it('task_deleted 缺 task_id 时不动缓存，仅 bump 版本号', () => {
    queryClient.setQueryData(queryKeys.longTermTasks, [{ id: 't1' }] as never)
    const before = useLayoutModeStore.getState().workspaceDataVersion

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_SERVER_EVENTS.TASK_DELETED, {}))

    expect(queryClient.getQueryData(queryKeys.longTermTasks)).toHaveLength(1)
    expect(useLayoutModeStore.getState().workspaceDataVersion).toBe(before + 1)
    unmount()
  })

  it('task_deleted 删除的是活跃任务时清空 activeTaskId', () => {
    queryClient.setQueryData(queryKeys.longTermTasks, [{ id: 't1' }] as never)
    useLongTermTaskStore.setState({ activeTaskId: 't1' })

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_SERVER_EVENTS.TASK_DELETED, { task_id: 't1' }))

    expect(useLongTermTaskStore.getState().activeTaskId).toBeNull()
    unmount()
  })

  it('task_status_changed 只 bump 工作区版本号', () => {
    const before = useLayoutModeStore.getState().workspaceDataVersion
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, { anything: true }))
    expect(useLayoutModeStore.getState().workspaceDataVersion).toBe(before + 1)
    unmount()
  })
})

describe('useRealtimeEvents — pending_inputs_changed', () => {
  it('常规 action 同步全量队列到 pendingInputStore', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, {
        data: {
          pipeline_id: 'p1',
          action: 'enqueued',
          items: [{ id: 'i1', pipeline_id: 'p1', content: 'hello', source: 'user', created_at: '' }],
        },
      }),
    )

    expect(usePendingInputStore.getState().byPipeline['p1']).toHaveLength(1)
    expect(usePendingInputStore.getState().byPipeline['p1'][0].id).toBe('i1')
    unmount()
  })

  it('缺 pipeline_id 时直接返回（不同步任何管道）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, { data: { items: [] } }))
    expect(usePendingInputStore.getState().byPipeline).toEqual({})
    unmount()
  })

  it('data 缺省（事件体为空）时不抛错且无副作用', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    expect(() => act(() => emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, {}))).not.toThrow()
    expect(usePendingInputStore.getState().byPipeline).toEqual({})
    unmount()
  })

  it('action=returned：用户来源条目回填输入框、队列清空', () => {
    usePendingInputStore.setState({
      byPipeline: {
        p1: [{ id: 'old', pipeline_id: 'p1', content: '旧', source: 'user', created_at: '' }],
      },
      editingId: {},
    })

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, {
        data: {
          pipeline_id: 'p1',
          action: 'returned',
          items: [
            { id: 'i1', pipeline_id: 'p1', content: '第一条', source: 'user', created_at: '' },
            { id: 'i2', pipeline_id: 'p1', content: '第二条', source: 'user', created_at: '' },
          ],
        },
      }),
    )

    expect(useChatInputStore.getState().pendingInsert).toBe('第一条\n第二条')
    expect(usePendingInputStore.getState().byPipeline['p1']).toEqual([])
    unmount()
  })

  it('action=returned：非用户来源（trigger/task）条目被丢弃，不回填输入框', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, {
        data: {
          pipeline_id: 'p1',
          action: 'returned',
          items: [
            { id: 'i1', pipeline_id: 'p1', content: '子任务完成', source: 'task', created_at: '' },
          ],
        },
      }),
    )

    expect(useChatInputStore.getState().pendingInsert).toBeNull()
    expect(usePendingInputStore.getState().byPipeline['p1']).toEqual([])
    unmount()
  })

  it('action=returned：用户条目内容为空串时也不回填（过滤后无内容）', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, {
        data: {
          pipeline_id: 'p1',
          action: 'returned',
          items: [{ id: 'i1', pipeline_id: 'p1', content: '', source: 'user', created_at: '' }],
        },
      }),
    )

    expect(useChatInputStore.getState().pendingInsert).toBeNull()
    unmount()
  })

  it('action=returned：items 缺省时按空列表处理（清空队列、不回填）', () => {
    usePendingInputStore.setState({
      byPipeline: {
        p1: [{ id: 'old', pipeline_id: 'p1', content: '旧', source: 'user', created_at: '' }],
      },
      editingId: {},
    })

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, { data: { pipeline_id: 'p1', action: 'returned' } }),
    )

    expect(useChatInputStore.getState().pendingInsert).toBeNull()
    expect(usePendingInputStore.getState().byPipeline['p1']).toEqual([])
    unmount()
  })
})

describe('useRealtimeEvents — 通知类事件', () => {
  it('compression_failed 带后端 message 时原样展示', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_SERVER_EVENTS.COMPRESSION_FAILED, {
        data: { pipeline_id: 'p1', message: '模型返回 413' },
      }),
    )

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '上下文压缩失败')
    expect(notif?.message).toBe('模型返回 413')
    expect(notif?.priority).toBe('high')
    expect(notif?.sourceLabel).toBe('上下文压缩')
    unmount()
  })

  it('compression_failed 无 message 时使用缺省说明文案', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_SERVER_EVENTS.COMPRESSION_FAILED, {}))

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '上下文压缩失败')
    expect(notif?.message).toContain('上下文将持续膨胀')
    unmount()
  })

  it('kicked_by_replacement 发常驻（autoDismissMs=0）高优通知', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_LOCAL_EVENTS.KICKED_BY_REPLACEMENT, {}))

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '本页连接已被其他页面替换')
    expect(notif).toBeDefined()
    expect(notif?.autoDismissMs).toBe(0)
    expect(notif?.priority).toBe('high')
    unmount()
  })

  it('user_input_send_timeout 缺 pipeline_id 时只发通知，不动消息 store', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    const msgsBefore = usePipelineMessageStore.getState().messagesByPipeline

    act(() =>
      emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {
        data: { thread_id: 'th', client_message_id: 'cmid-x' },
      }),
    )

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '消息发送失败')
    expect(notif?.message).toContain('连接断开，消息未送达')
    expect(usePipelineMessageStore.getState().messagesByPipeline).toEqual(msgsBefore)
    unmount()
  })

  it('user_input_send_timeout 事件体缺 data 时用空对象兜底并走缺省 reason', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {}))

    const notif = useNotificationStore
      .getState()
      .notifications.find((n) => n.title === '消息发送失败')
    expect(notif?.message).toContain('连接断开，消息未送达')
    unmount()
  })

  it('user_input_send_timeout 缺 thread_id 时插入的 system 消息 sessionId 落空串', () => {
    const pipelineId = 'pipe-branch-3'
    const cmid = 'cmid-branch-3'
    const ps = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState({
      messagesByPipeline: { ...ps.messagesByPipeline, [pipelineId]: [] },
      streamingState: { ...ps.streamingState, [pipelineId]: undefined },
    })

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() =>
      emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {
        data: { pipeline_id: pipelineId, client_message_id: cmid },
      }),
    )

    const err = usePipelineMessageStore
      .getState()
      .getMessages(pipelineId)
      .find((m) => m.id === `send_failed_${cmid}`)
    expect(err?.sessionId).toBe('')
    unmount()
  })

  it('user_input_send_timeout 有 pipeline_id 无 cmid：停流式但不标记/插入消息', () => {
    const pipelineId = 'pipe-branch-1'
    const ps = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState({
      messagesByPipeline: { ...ps.messagesByPipeline, [pipelineId]: [] },
      streamingState: { ...ps.streamingState, [pipelineId]: undefined },
    })

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => usePipelineMessageStore.getState().startStreaming(pipelineId, 'cmid-live'))
    expect(usePipelineMessageStore.getState().streamingState[pipelineId]).toBeDefined()

    act(() =>
      emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {
        data: { thread_id: 'th', pipeline_id: pipelineId, reason: '超时' },
      }),
    )

    expect(usePipelineMessageStore.getState().streamingState[pipelineId]).toBeUndefined()
    expect(usePipelineMessageStore.getState().getMessages(pipelineId)).toEqual([])
    unmount()
  })

  it('user_input_send_timeout 有 pipeline_id 与 cmid：标记 failed、停流式、插 system 错误消息', () => {
    const pipelineId = 'pipe-branch-2'
    const cmid = 'cmid-branch-2'
    const ps = usePipelineMessageStore.getState()
    usePipelineMessageStore.setState({
      messagesByPipeline: { ...ps.messagesByPipeline, [pipelineId]: [] },
      streamingState: { ...ps.streamingState, [pipelineId]: undefined },
    })

    const { unmount } = renderHook(() => useRealtimeEvents())
    act(() => {
      ps.startStreaming(pipelineId, cmid)
      ps.addMessage(pipelineId, {
        id: cmid,
        sessionId: 'th',
        role: 'user',
        content: '测试',
        timestamp: new Date().toISOString(),
        status: 'sending',
        clientMessageId: cmid,
      } as never)
    })

    act(() =>
      emit(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, {
        data: {
          thread_id: 'th',
          pipeline_id: pipelineId,
          client_message_id: cmid,
          reason: '连接断开超过 20s',
        },
      }),
    )

    const msgs = usePipelineMessageStore.getState().getMessages(pipelineId)
    expect(msgs.find((m) => m.id === cmid)?.status).toBe('failed')
    const err = msgs.find((m) => m.id === `send_failed_${cmid}`)
    expect(err?.role).toBe('system')
    expect(err?.content).toContain('连接断开超过 20s')
    expect(err?.content).toContain('这条消息没有发到服务器')
    expect(usePipelineMessageStore.getState().streamingState[pipelineId]).toBeUndefined()
    unmount()
  })
})

describe('useRealtimeEvents — visibility 回前台重连', () => {
  /** 派发 visibilitychange 并把 visibilityState 设为指定值 */
  function fireVisibility(state: 'visible' | 'hidden') {
    Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  }

  afterEach(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  })

  it('页面不可见时不重连', async () => {
    wsState.status = 'disconnected'
    ensureFreshTokenMock.mockResolvedValue('tok')
    const { unmount } = renderHook(() => useRealtimeEvents())

    await act(async () => {
      fireVisibility('hidden')
    })

    expect(connectMock).not.toHaveBeenCalled()
    unmount()
  })

  it('连接仍为 connected 时不重连（不打断健康连接）', async () => {
    wsState.status = 'connected'
    ensureFreshTokenMock.mockResolvedValue('tok')
    const { unmount } = renderHook(() => useRealtimeEvents())

    await act(async () => {
      fireVisibility('visible')
    })

    expect(connectMock).not.toHaveBeenCalled()
    unmount()
  })

  it('已被 4000 踢旧时不自动重连（避免互踢环）', async () => {
    wsState.status = 'disconnected'
    wsState.kicked = true
    ensureFreshTokenMock.mockResolvedValue('tok')
    const { unmount } = renderHook(() => useRealtimeEvents())

    await act(async () => {
      fireVisibility('visible')
    })

    expect(connectMock).not.toHaveBeenCalled()
    unmount()
  })

  it('断线且取到新 token 时用该 token 重连', async () => {
    wsState.status = 'disconnected'
    wsState.kicked = false
    ensureFreshTokenMock.mockResolvedValue('fresh-token')
    const { unmount } = renderHook(() => useRealtimeEvents())

    await act(async () => {
      fireVisibility('visible')
    })

    expect(connectMock).toHaveBeenCalledWith('fresh-token')
    unmount()
  })

  it('token 刷新失败（返回 null）不重连，也不抛错', async () => {
    wsState.status = 'disconnected'
    wsState.kicked = false
    ensureFreshTokenMock.mockResolvedValue(null)
    const { unmount } = renderHook(() => useRealtimeEvents())

    await act(async () => {
      fireVisibility('visible')
    })

    expect(connectMock).not.toHaveBeenCalled()
    unmount()
  })

  it('ensureFreshToken reject 时静默兜底（交给既有重连机制）', async () => {
    wsState.status = 'disconnected'
    wsState.kicked = false
    ensureFreshTokenMock.mockRejectedValue(new Error('refresh failed'))
    const { unmount } = renderHook(() => useRealtimeEvents())

    await act(async () => {
      fireVisibility('visible')
    })
    await act(async () => {})

    expect(connectMock).not.toHaveBeenCalled()
    unmount()
  })

  it('unmount 后派发 visibilitychange 不再触发重连（监听器已摘除）', async () => {
    wsState.status = 'disconnected'
    ensureFreshTokenMock.mockResolvedValue('tok')
    const { unmount } = renderHook(() => useRealtimeEvents())
    unmount()

    await act(async () => {
      fireVisibility('visible')
    })

    expect(connectMock).not.toHaveBeenCalled()
  })
})
