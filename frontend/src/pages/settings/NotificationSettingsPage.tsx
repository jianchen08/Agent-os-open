/**
 * 通知设置页（设置中枢子页）
 *
 * 系统通知权限状态（已授权/已拒绝/未请求）+ 跳转系统通知设置 + 引导文案 +
 * 交互提示音选择（目录由 notification_sounds 插件递送，选择即试听，设备级持久化）。
 * 权限查询：壳内走 @capacitor/local-notifications 原生桩（与壳轮询脚本同一
 * 运行时桥），浏览器回落 Web Notifications API；原生跳转仅在壳内可用
 * （ShellIntents 插件，MainActivity 注册），无壳时隐藏按钮。
 */

import { useEffect, useState } from 'react'
import { PageShell } from '@/components/shared/PageShell'
import {
  getShellIntents,
  queryNotificationPermission,
  type NotificationPermissionState,
} from '@/services/shellPlugins'
import {
  getNotificationSoundId,
  getNotificationSoundOptions,
  playNotificationSound,
  setNotificationSoundId,
} from '@/utils/audioNotification'
import type { NotificationSoundPreset } from '@/services/api/notificationSounds'

const PERMISSION_LABELS: Record<NotificationPermissionState, string> = {
  granted: '已授权',
  denied: '已拒绝',
  prompt: '未请求',
  unsupported: '当前环境不支持系统通知',
}

export function NotificationSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const [permission, setPermission] = useState<NotificationPermissionState | null>(null)
  /** 壳内可用（ShellIntents 插件桩存在）；无壳隐藏跳转按钮 */
  const [canOpenSystemSettings, setCanOpenSystemSettings] = useState(false)
  /** 提示音目录（插件递送）；目录不可用时选择器退化为不可用文案 */
  const [soundOptions, setSoundOptions] = useState<NotificationSoundPreset[]>([])
  const [soundId, setSoundId] = useState('')

  useEffect(() => {
    setCanOpenSystemSettings(getShellIntents() !== null)
    void queryNotificationPermission().then(setPermission)
    setSoundId(getNotificationSoundId())
    void getNotificationSoundOptions().then((catalog) => {
      setSoundOptions(catalog.sounds)
      // 未显式选择过时展示插件默认项（不落盘：保持"未选择=跟随默认"语义）
      setSoundId((current) => current || catalog.defaultId)
    })
  }, [])

  const handleSoundChange = (id: string) => {
    setSoundId(id)
    setNotificationSoundId(id)
    void playNotificationSound() // 选择即试听
  }

  const content = (
    <>
      <section className="mb-8">
        <h2 className="mb-3 text-sm font-semibold">通知权限</h2>
        <div className="border-border rounded-lg border p-4">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                permission === 'granted'
                  ? 'bg-status-success'
                  : permission === 'denied'
                    ? 'bg-destructive'
                    : 'bg-muted-foreground/40'
              }`}
              data-testid="notification-status-dot"
            />
            <span className="text-sm" data-testid="notification-permission-state">
              {permission === null ? '查询中…' : PERMISSION_LABELS[permission]}
            </span>
          </div>
          {canOpenSystemSettings && (
            <button
              type="button"
              onClick={() => {
                void getShellIntents()?.openNotificationSettings()
              }}
              className="border-border hover:bg-muted/30 mt-3 rounded-lg border px-3 py-1.5 text-xs"
              data-testid="notification-open-system-settings"
            >
              打开系统通知设置
            </button>
          )}
        </div>
        <p className="text-muted-foreground mt-3 text-xs">
          开启悬浮通知/横幅后通知才会弹出。若显示「已拒绝」，请到系统通知设置中允许
          灵汐助手的通知权限；悬浮通知/横幅权限也在同一设置页开启。
        </p>
      </section>

      <section className="mb-8">
        <h2 className="mb-3 text-sm font-semibold">交互提示音</h2>
        <div className="border-border rounded-lg border p-4">
          {soundOptions.length > 0 ? (
            <div className="flex items-center gap-2">
              <select
                value={soundId}
                onChange={(e) => handleSoundChange(e.target.value)}
                className="border-border bg-background rounded-lg border px-3 py-1.5 text-sm"
                data-testid="notification-sound-select"
                aria-label="交互提示音"
              >
                {soundOptions.map((sound) => (
                  <option key={sound.id} value={sound.id}>
                    {sound.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => void playNotificationSound()}
                className="border-border hover:bg-muted/30 rounded-lg border px-3 py-1.5 text-xs"
                data-testid="notification-sound-preview"
              >
                试听
              </button>
            </div>
          ) : (
            <span className="text-muted-foreground text-sm" data-testid="notification-sound-unavailable">
              提示音目录不可用（notification_sounds 插件未加载）
            </span>
          )}
        </div>
        <p className="text-muted-foreground mt-3 text-xs">
          选择后立即生效并试听。音效目录由 notification_sounds 插件递送，向其
          sounds/ 目录添加自定义 JSON 预设即可扩充（无需改前端）。
        </p>
      </section>
    </>
  )

  if (embedded) {
    return (
      <PageShell title="通知设置" embedded>
        {content}
      </PageShell>
    )
  }
  return <PageShell title="通知设置">{content}</PageShell>
}
