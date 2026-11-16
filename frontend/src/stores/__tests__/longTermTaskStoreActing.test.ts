// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * longTermTaskStore 行级操作进行中集合（actingTaskIds）测试（#11 统一暴露）：
 * - 写动作（pause/resume/cancel/toggleAutoExecute）await 窗口内置位、落定清除
 *   （成功/失败皆清——失败不假装成功也不残留 disabled）
 * - setTaskActing 直连标记/清除（直连 API 的操作编排方消费面）
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLongTermTaskStore } from '../longTermTaskStore'

const apiMocks = vi.hoisted(() => ({
  toggleAutoExecute: vi.fn(),
  pauseLongTermTask: vi.fn(),
  resumeLongTermTask: vi.fn(),
  cancelLongTermTask: vi.fn(),
}))
vi.mock('@/services/api/longTermTasks', () => apiMocks)

const cacheMocks = vi.hoisted(() => ({ updateLongTermTasksCache: vi.fn() }))
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => cacheMocks)

const TASK = 't-acting'

describe('longTermTaskStore actingTaskIds（行级 pending 真值源）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useLongTermTaskStore.setState({ actingTaskIds: new Set(), activeTaskId: null })
  })

  it.each([
    ['pauseTask', 'pauseLongTermTask', ['t-acting'] as const],
    ['resumeTask', 'resumeLongTermTask', ['t-acting'] as const],
    ['cancelTask', 'cancelLongTermTask', ['t-acting'] as const],
    ['toggleAutoExecute', 'toggleAutoExecute', ['t-acting', true] as const],
  ])('%s await 窗口置位、落定清除', async (action, api, args) => {
    let resolveApi: (v: unknown) => void = () => {}
    apiMocks[api as keyof typeof apiMocks].mockReturnValue(
      new Promise((resolve) => {
        resolveApi = resolve
      }),
    )
    const store = useLongTermTaskStore.getState()
    const promise = (
      store[action as 'pauseTask'] as (...a: unknown[]) => Promise<unknown>
    )(...args)

    // await 窗口内置位（UI 行级 pending / 弹窗 confirming 据此渲染）
    expect(useLongTermTaskStore.getState().actingTaskIds.has(TASK)).toBe(true)

    resolveApi({})
    await promise
    expect(useLongTermTaskStore.getState().actingTaskIds.has(TASK)).toBe(false)
  })

  it.each([
    ['pauseTask', 'pauseLongTermTask', ['t-acting'] as const],
    ['resumeTask', 'resumeLongTermTask', ['t-acting'] as const],
    ['cancelTask', 'cancelLongTermTask', ['t-acting'] as const],
  ])('%s 失败：pending 清除且错误上抛（诚实状态机）', async (action, api, args) => {
    apiMocks[api as keyof typeof apiMocks].mockRejectedValueOnce(new Error('boom'))
    const store = useLongTermTaskStore.getState()
    await expect(
      (store[action as 'pauseTask'] as (...a: unknown[]) => Promise<unknown>)(...args),
    ).rejects.toThrow('boom')
    expect(useLongTermTaskStore.getState().actingTaskIds.has(TASK)).toBe(false)
  })

  it('setTaskActing 显式标记/清除（直连 API 编排方消费面；幂等清除不炸）', () => {
    const store = useLongTermTaskStore.getState()
    store.setTaskActing(TASK, true)
    expect(useLongTermTaskStore.getState().actingTaskIds.has(TASK)).toBe(true)
    store.setTaskActing(TASK, false)
    store.setTaskActing(TASK, false)
    expect(useLongTermTaskStore.getState().actingTaskIds.has(TASK)).toBe(false)
  })
})
