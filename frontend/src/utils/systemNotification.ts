/**
 * 系统级通知工具模块
 *
 * 经 Electron 主进程弹出宿主 OS 原生通知（Windows toast / macOS 通知中心 /
 * Linux libnotify，由主进程 Electron Notification 按系统路由）。
 *
 * - 仅 Electron 环境生效：浏览器（Web 构建）无 electronAPI 桥接，返回 false，
 *   提醒退化为应用内浮层 + Web Audio 提示音（见 audioNotification.ts）
 * - 尊重免打扰偏好：与提示音共用 notification_sound_muted 开关
 * - 永不 reject：任何失败（桥接缺失/调用异常）都折算为 false
 */

/**
 * 弹出系统通知。
 *
 * @param payload - title 通知标题（如交互请求标题）；body 通知正文（如请求描述 / 来源 Agent）
 * @returns 是否成功弹出（非 Electron / 已静音 / 宿主不支持 / 调用失败均为 false）
 */
export async function showSystemNotification(payload: {
  title: string
  body: string
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
