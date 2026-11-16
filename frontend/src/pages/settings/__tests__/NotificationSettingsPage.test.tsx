/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * NotificationSettingsPage — 权限状态显示（壳桩 / Web API / 不可用三分支）+
 * 原生跳转按钮仅在壳内可用 + 交互提示音选择器（目录渲染/选择即试听/不可用降级）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import {
  getNotificationSoundId,
  getNotificationSoundOptions,
  playNotificationSound,
  setNotificationSoundId,
} from '@/utils/audioNotification'
import { NotificationSettingsPage } from '../NotificationSettingsPage'

vi.mock('@/utils/audioNotification', () => ({
  getNotificationSoundId: vi.fn(() => ''),
  getNotificationSoundOptions: vi.fn(async () => ({ sounds: [], defaultId: '' })),
  playNotificationSound: vi.fn(async () => true),
  setNotificationSoundId: vi.fn(),
}))

const SOUND_CATALOG = {
  sounds: [
    { id: 'arpeggio', label: '大调琶音', default: true, params: { partials: [], notes: [] } },
    { id: 'dingdong_down', label: '下行叮咚', params: { partials: [], notes: [] } },
  ],
  defaultId: 'arpeggio',
}

function stubCapacitor(plugins: Record<string, unknown> | null) {
  Object.defineProperty(window, 'Capacitor', {
    value: plugins === null ? undefined : { Plugins: plugins },
    configurable: true,
    writable: true,
  })
}

afterEach(() => {
  stubCapacitor(null)
  vi.mocked(getNotificationSoundId).mockImplementation(() => '')
  vi.mocked(getNotificationSoundOptions).mockImplementation(async () => ({ sounds: [], defaultId: '' }))
  vi.restoreAllMocks()
})

describe('NotificationSettingsPage', () => {
  it('壳内 LocalNotifications.granted → 显示已授权 + 跳转按钮在位', async () => {
    const openSettings = vi.fn().mockResolvedValue(undefined)
    stubCapacitor({
      LocalNotifications: {
        checkPermissions: () => Promise.resolve({ display: 'granted' }),
      },
      ShellIntents: { openNotificationSettings: openSettings },
    })
    renderWithProviders(<NotificationSettingsPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('notification-permission-state')).toHaveTextContent('已授权')
    })
    fireEvent.click(screen.getByTestId('notification-open-system-settings'))
    expect(openSettings).toHaveBeenCalledTimes(1)
  })

  it('壳内 prompt → 未请求；denied → 已拒绝（区分度输入组）', async () => {
    stubCapacitor({
      LocalNotifications: {
        checkPermissions: () => Promise.resolve({ display: 'prompt' }),
      },
      ShellIntents: { openNotificationSettings: vi.fn() },
    })
    const { unmount } = renderWithProviders(<NotificationSettingsPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('notification-permission-state')).toHaveTextContent('未请求')
    })
    unmount()
    stubCapacitor({
      LocalNotifications: {
        checkPermissions: () => Promise.resolve({ display: 'denied' }),
      },
      ShellIntents: { openNotificationSettings: vi.fn() },
    })
    renderWithProviders(<NotificationSettingsPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('notification-permission-state')).toHaveTextContent('已拒绝')
    })
  })

  it('无壳（浏览器无 Notification API）→ 不支持 + 跳转按钮隐藏', async () => {
    stubCapacitor(null)
    renderWithProviders(<NotificationSettingsPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('notification-permission-state')).toHaveTextContent('不支持')
    })
    expect(screen.queryByTestId('notification-open-system-settings')).not.toBeInTheDocument()
  })

  it('提示音目录可用 → 选择器渲染目录项；未显式选择展示默认项但不落盘', async () => {
    vi.mocked(getNotificationSoundOptions).mockResolvedValue(SOUND_CATALOG)
    renderWithProviders(<NotificationSettingsPage embedded />)

    const select = await screen.findByTestId('notification-sound-select')
    expect(select).toHaveValue('arpeggio') // 未选择（''）→ 展示插件默认项
    expect(screen.getByRole('option', { name: '大调琶音' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '下行叮咚' })).toBeInTheDocument()
    expect(setNotificationSoundId).not.toHaveBeenCalled()
  })

  it('已显式选择的音效 → 选择器如实回显存储值', async () => {
    vi.mocked(getNotificationSoundOptions).mockResolvedValue(SOUND_CATALOG)
    vi.mocked(getNotificationSoundId).mockReturnValue('dingdong_down')
    renderWithProviders(<NotificationSettingsPage embedded />)

    await waitFor(() => {
      expect(screen.getByTestId('notification-sound-select')).toHaveValue('dingdong_down')
    })
  })

  it('切换音效 → 持久化所选 id 并立即试听', async () => {
    vi.mocked(getNotificationSoundOptions).mockResolvedValue(SOUND_CATALOG)
    renderWithProviders(<NotificationSettingsPage embedded />)

    const select = await screen.findByTestId('notification-sound-select')
    fireEvent.change(select, { target: { value: 'dingdong_down' } })
    expect(setNotificationSoundId).toHaveBeenCalledWith('dingdong_down')
    expect(playNotificationSound).toHaveBeenCalled()
    expect(select).toHaveValue('dingdong_down')
  })

  it('试听按钮 → 播放当前所选音效', async () => {
    vi.mocked(getNotificationSoundOptions).mockResolvedValue(SOUND_CATALOG)
    renderWithProviders(<NotificationSettingsPage embedded />)

    fireEvent.click(await screen.findByTestId('notification-sound-preview'))
    expect(playNotificationSound).toHaveBeenCalled()
  })

  it('目录不可用 → 选择器退化为不可用文案', async () => {
    vi.mocked(getNotificationSoundOptions).mockResolvedValue({ sounds: [], defaultId: '' })
    renderWithProviders(<NotificationSettingsPage embedded />)

    expect(await screen.findByTestId('notification-sound-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('notification-sound-select')).not.toBeInTheDocument()
  })
})
