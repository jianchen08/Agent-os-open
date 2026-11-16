/**
 * 系统级通知工具模块
 *
 * 出站：经 Electron 主进程弹出宿主 OS 原生通知（Windows toast / macOS 通知
 * 中心 / Linux libnotify，由主进程 Electron Notification 按系统路由）。
 * 入站：订阅壳层通知点击导航（notification:navigate），按 threadId 打开
 * 对应对话（10e799226 打开链：从 thread 重建会话条目后切换打开）。
 *
 * - 仅 Electron 环境生效：浏览器（Web 构建）无 electronAPI 桥接，出站返回
 *   false、入站为 no-op；提醒退化为应用内浮层 + Web Audio 提示音
 *   （见 audioNotification.ts）
 * - 出站尊重免打扰偏好：与提示音共用 notification_sound_muted 开关
 * - 出站永不 reject：任何失败（桥接缺失/调用异常）都折算为 false
 */

import { useSessionListStore } from '@/stores/sessionListStore'

/**
 * 弹出系统通知。
 *
 * @param payload - title 通知标题（如交互请求标题）；body 通知正文（如请求描述 /
 *                  来源 Agent）；threadId 可选，非空时点击通知跳转到该会话
 * @returns 是否成功弹出（非 Electron / 已静音 / 宿主不支持 / 调用失败均为 false）
 */
export async function showSystemNotification(payload: {
  title: string
  body: string
  threadId?: string
}): Promise<boolean> {
  if (typeof window === 'undefined') return false

  try {
    const muted = localStorage.getItem('notification_sound_muted')
    if (muted === 'true') return false
  } catch {
    // localStorage 不可用时按未静音继续（与提示音同口径）
  }

  const api = window.electronAPI?.notification
  if (!api) return false

  try {
    return await api.show(payload)
  } catch {
    return false
  }
}

/** 通知点击导航载荷合法性：仅非空字符串可导航，缺失/非法一律退化仅聚焦 */
function isActionableThreadId(threadId: unknown): threadId is string {
  return typeof threadId === 'string' && threadId.length > 0
}

/**
 * 系统通知点击 → 打开对应对话：按 threadId 重建会话条目（幂等，真实记录
 * 存在时不覆盖）后切换打开。打开失败只留痕不抛（窗口已被壳层聚焦，退化为
 * 仅聚焦，与无 threadId 同口径）。
 */
export function handleNotificationNavigate(
  payload: { threadId?: unknown } | undefined,
): void {
  const threadId = payload?.threadId
  if (!isActionableThreadId(threadId)) return
  const sessionList = useSessionListStore.getState()
  sessionList.rebuildSessionFromThread(threadId)
  void sessionList.setActiveSession(threadId).catch((e) => {
    console.error('[SystemNotification] 通知点击打开会话失败:', threadId, e)
  })
}

/**
 * 订阅壳层系统通知点击导航（应用装配一次性安装；Electron 专属，Web 无桥接
 * 或旧版壳无此通道时为 no-op）。返回取消订阅函数。
 */
export function installNotificationNavigateListener(): () => void {
  const api = window.electronAPI?.notification
  if (!api?.onNotificationNavigate) return () => {}
  return api.onNotificationNavigate(handleNotificationNavigate)
}
