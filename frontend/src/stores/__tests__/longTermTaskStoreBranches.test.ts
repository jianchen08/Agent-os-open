/**
 * longTermTaskStore 分支补测：四个写操作（成功回填 query 缓存 / 失败翻译错误消息）
 * + 选择态设置 + 事件增量写缓存 + 删除清理。
 *
 * 断言可观察行为：写入 TanStack Query 缓存的数据（经 readLongTermTasks 读回真值源）、
 * activeTaskId 状态、抛出的 Error 消息；mock 仅落在网络层（longTermTasks API）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { readLongTermTasks } from '@/hooks/queries/useLongTermTasksQuery'
import { useLongTermTaskStore } from '../longTermTaskStore'
import * as longTermTaskApi from '@/services/api/longTermTasks'
import type { Task } from '@/types/task'

vi.mock('@/services/api/longTermTasks', () => ({
  fetchLongTermTasks: vi.fn(),
  toggleAutoExecute: vi.fn(),
  pauseLongTermTask: vi.fn(),
  resumeLongTermTask: vi.fn(),
  cancelLongTermTask: vi.fn(),
}))

const makeTask = (id: string, extra: Partial<Task> = {}): Task =>
  ({
    id,
    goal: `goal-${id}`,
    status: 'running',
    tags: [],
    ...extra,
  }) as Task

/** 预置 query 缓存（真值源）并复位选择态 */
function seedCache(tasks: Task[]) {
  queryClient.setQueryData<Task[]>(queryKeys.longTermTasks, tasks)
  useLongTermTaskStore.setState({ activeTaskId: null })
}

beforeEach(() => {
  vi.clearAllMocks()
  queryClient.clear()
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('longTermTaskStore — 成功路径回填 query 缓存', () => {
  it('toggleAutoExecute 用返回值原位替换目标任务，其他任务不动', async () => {
    seedCache([makeTask('t1'), makeTask('t2')])
    const updated = makeTask('t1', { tags: ['auto-execute'] })
    vi.mocked(longTermTaskApi.toggleAutoExecute).mockResolvedValue(updated)

    await useLongTermTaskStore.getState().toggleAutoExecute('t1', true)

    const cached = readLongTermTasks()
    expect(cached.find((t) => t.id === 't1')!.tags).toEqual(['auto-execute'])
    expect(cached.find((t) => t.id === 't2')!.tags).toEqual([])
    expect(longTermTaskApi.toggleAutoExecute).toHaveBeenCalledWith('t1', true)
  })

  it('pauseTask 回填 blocked 状态', async () => {
    seedCache([makeTask('t1', { status: 'running' }), makeTask('t2', { status: 'running' })])
    vi.mocked(longTermTaskApi.pauseLongTermTask).mockResolvedValue(
      makeTask('t1', { status: 'blocked' }),
    )

    await useLongTermTaskStore.getState().pauseTask('t1')

    const cached = readLongTermTasks()
    expect(cached.find((t) => t.id === 't1')!.status).toBe('blocked')
    expect(cached.find((t) => t.id === 't2')!.status).toBe('running')
  })

  it('resumeTask 回填 running 状态', async () => {
    seedCache([makeTask('t1', { status: 'blocked' }), makeTask('t2', { status: 'running' })])
    vi.mocked(longTermTaskApi.resumeLongTermTask).mockResolvedValue(
      makeTask('t1', { status: 'running' }),
    )

    await useLongTermTaskStore.getState().resumeTask('t1')

    const cached = readLongTermTasks()
    expect(cached.find((t) => t.id === 't1')!.status).toBe('running')
    expect(cached.find((t) => t.id === 't2')!.id).toBe('t2')
  })

  it('cancelTask 无条件把状态归一为 stopped（后端 STOPPED 语义），非目标任务不动', async () => {
    seedCache([makeTask('t1', { status: 'running' }), makeTask('t2', { status: 'running' })])
    // 后端返回体不含 status 也要落 stopped
    vi.mocked(longTermTaskApi.cancelLongTermTask).mockResolvedValue({ id: 't1' } as Partial<Task>)

    await useLongTermTaskStore.getState().cancelTask('t1', '用户取消')

    const cached = readLongTermTasks()
    expect(cached.find((t) => t.id === 't1')!.status).toBe('stopped')
    expect(cached.find((t) => t.id === 't2')!.status).toBe('running')
    expect(longTermTaskApi.cancelLongTermTask).toHaveBeenCalledWith('t1', '用户取消')
  })
})

describe('longTermTaskStore — 失败路径错误翻译', () => {
  it('Error 携带 response.data.message 时优先用后端消息', async () => {
    seedCache([])
    const err = new Error('Request failed')
    ;(err as unknown as { response: { data: { message: string } } }).response = {
      data: { message: '后端拒绝：任务已结束' },
    }
    vi.mocked(longTermTaskApi.toggleAutoExecute).mockRejectedValue(err)

    await expect(useLongTermTaskStore.getState().toggleAutoExecute('t1', true)).rejects.toThrow(
      '后端拒绝：任务已结束',
    )
  })

  it('Error 无 response 时回退 error.message', async () => {
    seedCache([])
    vi.mocked(longTermTaskApi.pauseLongTermTask).mockRejectedValue(new Error('网络中断'))

    await expect(useLongTermTaskStore.getState().pauseTask('t1')).rejects.toThrow('网络中断')
  })

  it('Error 的 message 为空串时回退默认消息（|| 链末端）', async () => {
    seedCache([])
    vi.mocked(longTermTaskApi.resumeLongTermTask).mockRejectedValue(new Error(''))

    await expect(useLongTermTaskStore.getState().resumeTask('t1')).rejects.toThrow(
      '恢复长期任务失败',
    )
  })

  it('非 Error 抛出物（字符串）走默认消息', async () => {
    seedCache([])
    vi.mocked(longTermTaskApi.cancelLongTermTask).mockRejectedValue('boom')

    await expect(useLongTermTaskStore.getState().cancelTask('t1')).rejects.toThrow(
      '取消长期任务失败',
    )
  })

  it('失败时不修改缓存（性质：失败前后缓存内容一致）', async () => {
    seedCache([makeTask('t1', { status: 'running' })])
    vi.mocked(longTermTaskApi.pauseLongTermTask).mockRejectedValue(new Error('失败'))

    await expect(useLongTermTaskStore.getState().pauseTask('t1')).rejects.toThrow()
    expect(readLongTermTasks().map((t) => [t.id, t.status])).toEqual([['t1', 'running']])
  })
})

describe('longTermTaskStore — 选择态与缓存增量', () => {
  it('setActiveTask 设置与清空', () => {
    useLongTermTaskStore.getState().setActiveTask('t1')
    expect(useLongTermTaskStore.getState().activeTaskId).toBe('t1')
    useLongTermTaskStore.getState().setActiveTask(null)
    expect(useLongTermTaskStore.getState().activeTaskId).toBeNull()
  })

  it('updateTask 部分合并目标任务（其余任务保持原值）', () => {
    seedCache([makeTask('t1'), makeTask('t2')])
    useLongTermTaskStore.getState().updateTask('t1', { status: 'blocked' })
    const cached = readLongTermTasks()
    expect(cached.find((t) => t.id === 't1')!.status).toBe('blocked')
    expect(cached.find((t) => t.id === 't2')!.status).toBe('running')
  })

  it('updateTask 对不存在的任务 id 是无副作用（性质：缓存长度不变）', () => {
    seedCache([makeTask('t1')])
    useLongTermTaskStore.getState().updateTask('ghost', { status: 'stopped' })
    expect(readLongTermTasks()).toHaveLength(1)
    expect(readLongTermTasks()[0].status).toBe('running')
  })

  it('updateTask 无缓存时以空表为基线（不抛错）', () => {
    queryClient.clear()
    expect(() =>
      useLongTermTaskStore.getState().updateTask('t1', { status: 'blocked' }),
    ).not.toThrow()
    expect(readLongTermTasks()).toEqual([])
  })

  it('deleteTask 从缓存移除该任务', () => {
    seedCache([makeTask('t1'), makeTask('t2')])
    useLongTermTaskStore.getState().deleteTask('t1')
    expect(readLongTermTasks().map((t) => t.id)).toEqual(['t2'])
  })

  it('deleteTask 删除活跃任务时清空 activeTaskId', () => {
    seedCache([makeTask('t1'), makeTask('t2')])
    useLongTermTaskStore.getState().setActiveTask('t1')
    useLongTermTaskStore.getState().deleteTask('t1')
    expect(useLongTermTaskStore.getState().activeTaskId).toBeNull()
  })

  it('deleteTask 删除非活跃任务时保留 activeTaskId', () => {
    seedCache([makeTask('t1'), makeTask('t2')])
    useLongTermTaskStore.getState().setActiveTask('t1')
    useLongTermTaskStore.getState().deleteTask('t2')
    expect(useLongTermTaskStore.getState().activeTaskId).toBe('t1')
    expect(readLongTermTasks().map((t) => t.id)).toEqual(['t1'])
  })
})

describe('longTermTaskStore — 持久化范围', () => {
  it('仅持久化 activeTaskId（tasks 数据不进 localStorage）', () => {
    useLongTermTaskStore.getState().setActiveTask('t9')
    const raw = localStorage.getItem('long-term-task-storage')
    expect(raw).toBeTruthy()
    const parsed = JSON.parse(raw!)
    expect(parsed.state).toEqual({ activeTaskId: 't9' })
  })
})
