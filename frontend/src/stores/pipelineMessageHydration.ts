/**
 * 管道消息 IndexedDB rehydrate 完成信号（自 pipelineMessageStore 抽出，
 * 巨型文件冻结门禁：只许缩小不许增长）。
 *
 * persist onFinishHydration 设置完成标记；刷新后首屏 loadPipelineMessages
 * 在 rehydrate 前到达时等待，否则本地缓存不可见 → auto 判定"未对账"全量
 * init，IndexedDB 缓存形同虚设。
 */
let _hydrated = false
const _hydrationWaiters: Array<() => void> = []

/** persist onFinishHydration 回调：标记完成并唤醒全部等待者 */
export const markMessageHydrated = (): void => {
  _hydrated = true
  for (const resolve of _hydrationWaiters.splice(0)) resolve()
}

/** 等待 persist rehydrate 完成（已完成立即返回；500ms 兜底防极端情况挂起） */
export function waitForMessageHydration(): Promise<void> {
  if (_hydrated) return Promise.resolve()
  return new Promise((resolve) => {
    _hydrationWaiters.push(resolve)
    // 兜底：rehydrate 未触发（存储异常等）也不阻塞加载，缓存恢复退化为全量 API
    window.setTimeout(() => {
      const idx = _hydrationWaiters.indexOf(resolve)
      if (idx !== -1) _hydrationWaiters.splice(idx, 1)
      resolve()
    }, 500)
  })
}
