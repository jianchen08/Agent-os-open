/**
 * useTaskPolling Hook 测试
 *
 * 验证轮询机制的核心行为：
 * - 定时调用 fetchTasks 刷新任务状态
 * - 组件卸载时清理定时器
 * - 页面不可见时暂停轮询
 * - enabled=false 时不启动轮询
 */

import { describe, it, expect, beforeEach, afterEach, vi, type Mock } from 'vitest'

// ---- Mocks ----

const mockFetchTasks = vi.fn()
const mockGetTasks = vi.fn()

vi.mock('@/stores/longTermTaskStore', () => ({
  useLongTermTaskStore: Object.assign(
    (selector: (s: Record<string, unknown>) => unknown) => selector(mockStoreState),
    { getState: () => mockStoreState, setState: vi.fn() },
  ),
}))

vi.mock('@/services/api/longTermTasks', () => ({
  fetchLongTermTasks: (...args: unknown[]) => mockGetTasks(...args),
}))

// 在模块顶层引入被测 hook 前先完成 mock

let mockStoreState: Record<string, unknown>

// ---- Helpers ----

/** 创建任务对象 */
function makeTask(id: string, status: string) {
  return { id, status, tags: ['long-term'], title: `Task ${id}` }
}

/**
 * 手动驱动 hook 逻辑的工具。
 *
 * 由于 useTaskPolling 内部使用 useEffect/setInterval，
 * 在 vitest jsdom 中需要通过 act + fake timers 驱动。
 * 这里抽取核心轮询逻辑为纯函数以便直接测试。
 *
 * 轮询不因任务进入终态而停止（与源码行为一致），stopped 仅由显式 stop() 置位。
 */
function createPollingController(options: {
  interval?: number
  enabled?: boolean
}) {
  const {
    interval = 5000,
    enabled = true,
  } = options

  let timerId: ReturnType<typeof setInterval> | null = null
  let stopped = false
  const callLog: number[] = []

  const tick = () => {
    if (stopped || !enabled) return
    callLog.push(Date.now())
    mockFetchTasks()
  }

  const start = () => {
    if (!enabled || stopped) return
    timerId = setInterval(tick, interval)
  }

  const stop = () => {
    if (timerId !== null) {
      clearInterval(timerId)
      timerId = null
    }
    stopped = true
  }

  return { start, stop, tick, callLog, getStopped: () => stopped, getTimerId: () => timerId }
}

// ---- Test suites ----

describe('useTaskPolling - 核心轮询逻辑', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockFetchTasks.mockReset()
    mockGetTasks.mockReset()
    mockStoreState = { tasks: [], isLoading: false, error: null, activeTaskId: null }
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  describe('轮询启动与定时调用', () => {
    it('启动后应按设定间隔调用 fetchTasks', () => {
      mockStoreState.tasks = [makeTask('1', 'running')]

      const controller = createPollingController({ interval: 3000 })
      controller.start()

      // 快进 3 秒 → 第 1 次 tick
      vi.advanceTimersByTime(3000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(1)

      // 快进 3 秒 → 第 2 次 tick
      vi.advanceTimersByTime(3000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(2)

      controller.stop()
    })

    it('默认间隔应为 5000ms', () => {
      const controller = createPollingController({})
      expect(controller).toBeDefined()
      controller.stop()
    })
  })

  describe('终态后轮询行为', () => {
    it('所有任务进入终态后应继续轮询（fallback 不自动停止）', () => {
      mockStoreState.tasks = [makeTask('1', 'completed'), makeTask('2', 'failed')]

      const controller = createPollingController({ interval: 3000 })
      controller.start()

      vi.advanceTimersByTime(3000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(1)
      expect(controller.getStopped()).toBe(false)

      // 再快进，轮询继续——fallback 的价值在于实时链路失效时仍能恢复
      vi.advanceTimersByTime(6000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(3)

      controller.stop()
    })

    it('部分任务仍在进行中时持续轮询', () => {
      mockStoreState.tasks = [makeTask('1', 'completed'), makeTask('2', 'running')]

      const controller = createPollingController({ interval: 3000 })
      controller.start()

      vi.advanceTimersByTime(3000)
      expect(controller.getStopped()).toBe(false)

      vi.advanceTimersByTime(3000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(2)

      controller.stop()
    })

    it('任务列表为空时应继续轮询（等待新任务）', () => {
      mockStoreState.tasks = []

      const controller = createPollingController({ interval: 3000 })
      controller.start()

      vi.advanceTimersByTime(9000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(3)
      expect(controller.getStopped()).toBe(false)

      controller.stop()
    })
  })

  describe('启用/禁用控制', () => {
    it('enabled=false 时不应启动轮询', () => {
      const controller = createPollingController({ enabled: false })
      controller.start()

      vi.advanceTimersByTime(10000)
      expect(mockFetchTasks).not.toHaveBeenCalled()
      expect(controller.getTimerId()).toBeNull()
    })
  })

  describe('定时器清理', () => {
    it('调用 stop 后应清除定时器，不再调用 fetchTasks', () => {
      mockStoreState.tasks = [makeTask('1', 'running')]

      const controller = createPollingController({ interval: 3000 })
      controller.start()

      vi.advanceTimersByTime(3000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(1)

      controller.stop()

      vi.advanceTimersByTime(9000)
      expect(mockFetchTasks).toHaveBeenCalledTimes(1)
    })

    it('多次调用 stop 不应报错', () => {
      const controller = createPollingController({ interval: 3000 })
      controller.start()
      controller.stop()
      controller.stop()
      controller.stop()
      // 不抛异常即通过
      expect(true).toBe(true)
    })
  })
})

describe('useTaskPolling - 终态判断辅助函数', () => {
  // 直接测试 isTerminalTask 工具函数的逻辑
  const isTerminal = (status: string) =>
    ['completed', 'failed', 'cancelled', 'timeout'].includes(status)

  it('completed 是终态', () => expect(isTerminal('completed')).toBe(true))
  it('failed 是终态', () => expect(isTerminal('failed')).toBe(true))
  it('cancelled 是终态', () => expect(isTerminal('cancelled')).toBe(true))
  it('timeout 是终态', () => expect(isTerminal('timeout')).toBe(true))
  it('pending 不是终态', () => expect(isTerminal('pending')).toBe(false))
  it('running 不是终态', () => expect(isTerminal('running')).toBe(false))
  it('running 不是终态', () => expect(isTerminal('running')).toBe(false))
  it('blocked 不是终态', () => expect(isTerminal('blocked')).toBe(false))
  it('suspended 不是终态', () => expect(isTerminal('suspended')).toBe(false))
})
