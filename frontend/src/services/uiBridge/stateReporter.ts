/**
 * uiBridge 状态上报调度器（冻结件 §2.3 createReportScheduler）
 *
 * 触发时机语义单点：store 变更防抖合并（默认 500ms，窗口随每次 notify 重置）、
 * WS 重连立即上报、心跳周期保活（默认 30s，插件缓存 freshness 窗口靠它自愈）。
 * send 抛错（同步或 Promise 拒绝）一律不外抛，下一触发点自然重试。
 *
 * 快照体由注入的 send 闭包自取（§2.3：send 是唯一外发点，snapshot 形参为
 * 调度器占位——本组件只承载触发时机，生产接线在挂载切片闭合快照来源）。
 *
 * @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P0感知闭环 | @vision V2 全能闭环 | @ci frontend-test
 */

/** 调度器选项（§2.3 逐字：send/now 必注入，防抖与心跳可覆盖默认值） */
export interface ReportSchedulerOptions {
  /** 注入的外发函数（唯一 mock 点） */
  send: (snapshot: object) => Promise<void>
  /** 注入时钟 */
  now: () => number
  /** 防抖窗口，默认 500ms */
  debounceMs?: number
  /** 心跳周期，默认 30000ms */
  heartbeatMs?: number
}

/** 调度器句柄（§2.3 逐字：notifyChange / onReconnect / stop） */
export interface ReportScheduler {
  notifyChange(): void
  onReconnect(): void
  stop(): void
}

const DEFAULT_DEBOUNCE_MS = 500
const DEFAULT_HEARTBEAT_MS = 30_000

export function createReportScheduler(opts: ReportSchedulerOptions): ReportScheduler {
  const debounceMs = opts.debounceMs ?? DEFAULT_DEBOUNCE_MS
  const heartbeatMs = opts.heartbeatMs ?? DEFAULT_HEARTBEAT_MS
  const send = opts.send
  // opts.now 为 §2.3 契约位（注入时钟）：触发语义纯定时器驱动，当前不读时刻

  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null
  let stopped = false

  const clearDebounce = () => {
    if (debounceTimer !== null) {
      clearTimeout(debounceTimer)
      debounceTimer = null
    }
  }

  const doSend = () => {
    if (stopped) return
    try {
      // 快照占位对象（见文件头注）：失败不外抛，下一触发点自然重试
      const result = send({})
      if (result && typeof result.catch === 'function') {
        result.catch(() => undefined)
      }
    } catch {
      // 同步抛错同样吞掉
    }
  }

  const scheduleHeartbeat = () => {
    heartbeatTimer = setTimeout(() => {
      doSend()
      scheduleHeartbeat()
    }, heartbeatMs)
  }

  scheduleHeartbeat()

  return {
    notifyChange() {
      if (stopped) return
      clearDebounce()
      debounceTimer = setTimeout(() => {
        debounceTimer = null
        doSend()
      }, debounceMs)
    },
    onReconnect() {
      if (stopped) return
      clearDebounce() // 挂起中的防抖合并进立即发送，不双发
      doSend()
    },
    stop() {
      stopped = true
      clearDebounce()
      if (heartbeatTimer !== null) {
        clearTimeout(heartbeatTimer)
        heartbeatTimer = null
      }
    },
  }
}
