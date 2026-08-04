/**
 * 持久化写入节流器（共享工具）
 *
 * 流式消息等场景会高频触发 setItem，逐次同步/异步落盘代价高且无意义
 * （中间态会被后续覆盖）。本工具把窗口内的多次写入合并为最后一次，
 * trailing 定时器统一落盘；持续高频时按 maxWait 强制落盘避免无限推迟。
 *
 * 每个调用方通过 createPersistThrottle() 取得独立实例，
 * 状态互不干扰（不同 store / 不同存储介质各自节流）。
 */

/** 窗口内新写入合并，到点统一落盘（流式抖动合并阈值） */
const PERSIST_THROTTLE_MS = 1000
/** 持续高频写入时强制落盘的上限：避免长流式（>1s）期间缓冲永远推迟落盘。
 * 取 5s：远大于单帧间隔，能合并绝大部分流式抖动；又足够短，崩溃时最多丢 5s。 */
const PERSIST_MAX_WAIT_MS = 5000

/** 实际落盘动作：由调用方注入（同步或异步均可，错误自行处理） */
export type PersistWriter = (name: string, value: string) => void | Promise<void>

interface ThrottleState {
  /** 缓冲的最新一次待写入（窗口内多次 set 只保留最后一次） */
  buffer: { name: string; value: string }
  /** trailing 定时器句柄 */
  timer: ReturnType<typeof setTimeout> | null
  /** 当前节流窗口起点（首次进入窗口的时刻） */
  windowStartedAt: number
}

/**
 * 创建一个独立的持久化节流器。
 *
 * 用法：
 *   const throttle = createPersistThrottle((name, value) => idb.set(name, value))
 *   throttle.schedule('pipeline-messages', json)  // 高频调用只触发一次落盘
 *   throttle.cancel()                              // 清理时取消挂起写入
 */
export function createPersistThrottle(write: PersistWriter) {
  const state: ThrottleState = {
    buffer: { name: '', value: '' },
    timer: null,
    windowStartedAt: 0,
  }

  /** 取出缓冲的最后一次写入执行落盘 */
  function flush(): void {
    state.timer = null
    state.windowStartedAt = 0
    const { name, value } = state.buffer
    state.buffer.name = ''
    state.buffer.value = ''
    if (!name) return
    void write(name, value)
  }

  /** 调度一次节流落盘。
   * - 窗口内已有挂起写入：推迟到当前窗口结束（合并）；
   *   若已达 maxWait 上限则强制落盘（防止流式期间永远推迟）。
   * - 无挂起写入：开启新窗口，trailing 定时器到点落盘。 */
  function schedule(name: string, value: string): void {
    state.buffer.name = name
    state.buffer.value = value
    if (state.timer !== null) {
      if (Date.now() - state.windowStartedAt >= PERSIST_MAX_WAIT_MS) {
        clearTimeout(state.timer)
        flush()
      }
      return
    }
    state.windowStartedAt = Date.now()
    state.timer = setTimeout(flush, PERSIST_THROTTLE_MS)
  }

  /** 取消挂起的写入（清理 / removeItem 时调用，避免 remove 后又被 trailing 写回） */
  function cancel(): void {
    if (state.timer !== null) {
      clearTimeout(state.timer)
      state.timer = null
    }
    state.windowStartedAt = 0
    state.buffer.name = ''
    state.buffer.value = ''
  }

  return { schedule, cancel, flush }
}
