/**
 * 壳插件类型化访问器（window.Capacitor.Plugins 安全面）。
 *
 * 与壳层 shell-push.js 同一运行时桥模式：不引 @capacitor/* npm 依赖，
 * 经 native-bridge 生成的插件桩调用原生方法（桩只在壳 WebView 存在）。
 * 浏览器/桌面无壳 → 各访问器返回 null，调用方降级（隐藏原生专属控件）。
 */

/** @capacitor/local-notifications 权限面（壳内原生插件桩的方法子集） */
interface ShellLocalNotificationsPlugin {
  checkPermissions(): Promise<{ display: 'granted' | 'denied' | 'prompt' }>
}

/** 壳自建插件（ShellIntents）：系统通知设置跳转 + 主题持久化（上报/查询） */
interface ShellIntentsPlugin {
  openNotificationSettings(): Promise<void>
  notifyShellTheme(args: { themeId: string }): Promise<void>
  getLastTheme(): Promise<{ themeId?: string }>
}

export type NotificationPermissionState = 'granted' | 'denied' | 'prompt' | 'unsupported'

function shellPlugin<T>(name: string): T | null {
  // 非 DOM 环境（node 侧单测/SSR）无 window，与「无壳」同样降级
  const plugins = typeof window === 'undefined' ? undefined : window.Capacitor?.Plugins
  if (!plugins) return null
  const p = plugins[name]
  return p && typeof p === 'object' ? (p as T) : null
}

export function getShellLocalNotifications(): ShellLocalNotificationsPlugin | null {
  return shellPlugin<ShellLocalNotificationsPlugin>('LocalNotifications')
}

export function getShellIntents(): ShellIntentsPlugin | null {
  return shellPlugin<ShellIntentsPlugin>('ShellIntents')
}

/** 壳自建插件（KernelHost）：本机内核宿主状态、本机登录凭据与后端模式生命周期 */
interface ShellKernelHostPlugin {
  status(): Promise<{ running?: boolean; port?: string; seeded?: boolean; initialized?: boolean }>
  localCredentials(): Promise<{ username?: string; password?: string }>
  setBackendMode(args: { mode: 'local' | 'remote' }): Promise<{ mode?: string }>
  /**
   * 登录结果上报（S5-A③ 漂移自愈计数面；旧包壳无此桩，消费方须先判存在）。
   * kind=失败类别（原生类别闸只对 credential 计数；HTTP 状态唯一可见面=
   * axios 层，判类后过桥，旧包前端不带 kind 原生按可用性不计）。
   */
  reportLoginOutcome(args: {
    success: boolean
    kind?: 'credential' | 'availability'
  }): Promise<void>
}

export function getShellKernelHost(): ShellKernelHostPlugin | null {
  return shellPlugin<ShellKernelHostPlugin>('KernelHost')
}

/** Capacitor 插件监听句柄（壳桩返回面；remove 兼容 Promise/void 两代桥） */
interface ShellSpeechListenerHandle {
  remove: () => Promise<void> | void
}

/** 壳自建插件（ShellSpeech）：Android 系统 SpeechRecognizer 桥（原生语音识别）。
 * 事件词表：speechPartial/speechFinal（transcript）+ speechError（code，与
 * Web Speech 错误码同形）；旧包壳无此桩 → 访问器返回 null，调用方回退 server-asr。 */
export interface ShellSpeechPlugin {
  available(): Promise<{ available?: boolean }>
  start(args?: { language?: string }): Promise<void>
  stop(): Promise<void>
  cancel(): Promise<void>
  addListener(
    eventName: 'speechPartial',
    listenerFunc: (data: { transcript?: string }) => void,
  ): Promise<ShellSpeechListenerHandle>
  addListener(
    eventName: 'speechFinal',
    listenerFunc: (data: { transcript?: string }) => void,
  ): Promise<ShellSpeechListenerHandle>
  addListener(
    eventName: 'speechError',
    listenerFunc: (data: { code?: string }) => void,
  ): Promise<ShellSpeechListenerHandle>
}

export function getShellSpeech(): ShellSpeechPlugin | null {
  return shellPlugin<ShellSpeechPlugin>('ShellSpeech')
}

/**
 * 通知权限状态统一查询：壳内走原生插件；浏览器回落 Web Notifications API
 * （'default' 与原生 'prompt' 同义 = 未请求）。两边都不可用 = unsupported。
 */
export async function queryNotificationPermission(): Promise<NotificationPermissionState> {
  const ln = getShellLocalNotifications()
  if (ln) {
    try {
      const st = await ln.checkPermissions()
      return st?.display === 'granted' || st?.display === 'denied' || st?.display === 'prompt'
        ? st.display
        : 'unsupported'
    } catch {
      return 'unsupported'
    }
  }
  if (typeof Notification !== 'undefined') {
    return Notification.permission === 'default' ? 'prompt' : Notification.permission
  }
  return 'unsupported'
}

/**
 * 主题变更上报壳层：壳把最近主题 id 持久化在应用私有 SharedPreferences
 * （WebView localStorage 被清/换源丢数据时由壳回写恢复，见 MainActivity）。
 * 浏览器/桌面无壳桥 → 直接返回（整条链路不激活，零影响）；桥调用失败
 * 只影响重装恢复，不阻塞切换本身。
 */
export async function notifyShellTheme(themeId: string): Promise<void> {
  const plugin = getShellIntents()
  if (!plugin?.notifyShellTheme) return
  try {
    await plugin.notifyShellTheme({ themeId })
  } catch {
    // 上报失败不重试不告警：下次页面加载壳会从 localStorage 重拉兜底
  }
}

/**
 * 查询壳持久化的最近主题 id：启动时 localStorage 无 theme-storage（新装/
 * 被清/孤儿化）时作恢复数据源（消费方 themeStore.initializeTheme）。浏览器/
 * 桌面无壳桥或桥调用失败 → null（调用方跳过恢复，落默认浅色 = 无壳现状）。
 */
export async function getShellLastTheme(): Promise<string | null> {
  const plugin = getShellIntents()
  if (!plugin?.getLastTheme) return null
  try {
    const result = await plugin.getLastTheme()
    const themeId = result?.themeId
    return typeof themeId === 'string' && themeId.length > 0 ? themeId : null
  } catch {
    return null
  }
}
