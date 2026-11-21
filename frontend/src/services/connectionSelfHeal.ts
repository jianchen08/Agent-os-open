/**
 * 连接池毒化自愈·渲染侧请求服务（2026-11-18 装机事故批次 2）。
 *
 * 背景：Electron NetworkService 对内核端口的死 ESTABLISHED socket 毒化后，
 * WS 重连循环全部卡死且永不自愈（内核健康、升级请求到不了内核）。渲染侧
 * GlobalWebSocket 重连计数达降级阈值（RECONNECT_DEGRADE_ATTEMPTS）时经本
 * 服务请求主进程自愈：主进程先探内核健康（健康=毒化在前端网络池才重建），
 * 通过才 session.closeAllConnections() 重建并广播 rebuilt；冷却限频
 * （5 分钟至多一次）与健康门控都在主进程侧，本服务只做触发面。
 *
 * 无 electronAPI（浏览器/dev/移动壳）→ no-op：直连形态无网络服务池毒化
 * 语义，重连循环自身即完整自愈路径。
 */
import { RECONNECT_DEGRADE_ATTEMPTS } from '@/constants/websocket'

interface ConnectionSelfHealApi {
  request(payload: { reconnectAttempts: number }): Promise<{
    rebuilt: boolean
    reason?: string
  }>
  onRebuilt(callback: (payload: { rebuiltAt: string }) => void): () => void
}

function selfHealApi(): ConnectionSelfHealApi | null {
  const api = (
    window as unknown as {
      electronAPI?: { connectionSelfHeal?: ConnectionSelfHealApi }
    }
  ).electronAPI?.connectionSelfHeal
  return api ?? null
}

/**
 * 重连失败计数达降级阈值时请求主进程自愈。
 *
 * 触发节拍 = 每满阈值次请求一轮（第 10/20/30… 次）：重连退避已到 30s 上限，
 * 两轮请求间隔 ≈5 分钟，与主进程冷却窗口（5 分钟）正好衔接；冷却内的重复
 * 请求由主进程 skip（不重建），渲染侧零状态。
 */
export function maybeRequestConnectionSelfHeal(reconnectAttempt: number): void {
  if (reconnectAttempt < RECONNECT_DEGRADE_ATTEMPTS) return
  if (reconnectAttempt % RECONNECT_DEGRADE_ATTEMPTS !== 0) return
  const api = selfHealApi()
  if (!api) return
  api.request({ reconnectAttempts: reconnectAttempt }).catch(() => {
    // 主进程不可用（非宿主形态/桥未就绪）→ 自愈不可用，重连循环继续兜底；
    // 失败原因随重连下一轮自然重试（吞错误登记：OBS-R258-1 同款无人消费 rejection）
  })
}

/** 订阅连接池已重建广播（常驻通知的挂载点消费）；无宿主能力 → no-op 解订阅 */
export function subscribeConnectionRebuilt(
  callback: (payload: { rebuiltAt: string }) => void,
): () => void {
  const api = selfHealApi()
  if (!api) return () => {}
  return api.onRebuilt(callback)
}
