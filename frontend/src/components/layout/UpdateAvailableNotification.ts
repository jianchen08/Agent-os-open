/**
 * 新版本可用提示 · 通知中心承载（P-24 更新提示面，不做自动更新）。
 *
 * 数据源=Electron 主进程启动时检查一次（electron/update-check.ts）：远端版本
 * 更高 → update:available 推送 + update:get-available 拉取兜底（渲染层挂载晚
 * 于推送时补拿）。本 hook 双通道消费：挂载拉一次 + 订阅后续推送，有新版本即
 * 通知中心一条条目（title「新版本可用」+ 版本号/下载地址随正文）——仅提示，
 * 不下载不安装（自动更新属装机环境动作，须用户确认）。
 *
 * 非 Electron 环境（dev 浏览器形态 window.electronAPI 缺失）→ no-op。
 * 通知样式/可关闭性由 NotificationCenter 现有面承载（isBlocking=false，
 * 用户升级前可关）。
 */
import { useEffect, useMemo, useState } from 'react'
import { useConditionNotifications, type ConditionNotification } from '@/hooks/useConditionNotifications'
import type { NotificationPriority } from '@/types/notification'

/** 主进程推送的新版本信息载荷（与 electron/update-check.ts UpdateInfo 对齐） */
export interface UpdateAvailableInfo {
  /** 清单宣告的最新版本号 */
  latestVersion: string
  /** 下载地址（主进程清单缺 download_url 时为空串） */
  downloadUrl: string
}

/** electronAPI.update 子 API（preload 暴露；测试桩按此形状注入） */
interface ElectronUpdateApi {
  getAvailable(): Promise<UpdateAvailableInfo | null>
  onAvailable(callback: (info: UpdateAvailableInfo) => void): () => void
}

function updateApi(): ElectronUpdateApi | undefined {
  return window.electronAPI?.update
}

/** 优先级：提示类条目用 high（警示黄，不占 critical 红） */
const NOTIFICATION_PRIORITY: NotificationPriority = 'high'

/**
 * UpdateInfo → 通知载荷：title「新版本可用」；正文含版本号；downloadUrl
 * 非空时追加地址段（空串不渲染空链接）。
 */
export function updateAvailableToNotificationInput(
  info: UpdateAvailableInfo,
): ConditionNotification['notification'] {
  const lines = [
    `检测到新版本 ${info.latestVersion}，当前安装的版本较旧。`,
    ...(info.downloadUrl ? [`下载地址：${info.downloadUrl}`] : []),
    '本应用不会自动更新；请下载后按同名同址覆盖安装。',
  ]
  return {
    title: '新版本可用',
    message: lines.join('\n'),
    category: 'info',
    priority: NOTIFICATION_PRIORITY,
    isBlocking: false,
    sourceLabel: '应用更新',
  }
}

/**
 * 新版本可用 → 通知中心同步（应用根组件常驻调用）：拉取 + 推送双通道驱动
 * 同一份 updateInfo 状态；条件非空（有新版本）→ 条目在列，null（同版本/
 * 未检查/环境无 IPC）→ 无条目。
 */
export function useUpdateAvailableNotification(resolveHoldMs?: number): void {
  const [updateInfo, setUpdateInfo] = useState<UpdateAvailableInfo | null>(null)

  useEffect(() => {
    const api = updateApi()
    if (!api) return
    let disposed = false
    const apply = (info: UpdateAvailableInfo | null): void => {
      if (!disposed) setUpdateInfo(info)
    }
    void api
      .getAvailable()
      .then(apply)
      .catch(() => {
        // IPC 不可用：保持无条目（检查面故障不打扰用户）
      })
    const unsubscribe = api.onAvailable(apply)
    return () => {
      disposed = true
      unsubscribe()
    }
  }, [])

  const conditions = useMemo(
    () =>
      updateInfo
        ? [
            {
              kind: 'update-available',
              notification: updateAvailableToNotificationInput(updateInfo),
            } satisfies ConditionNotification,
          ]
        : [],
    [updateInfo],
  )

  useConditionNotifications(conditions, resolveHoldMs)
}
