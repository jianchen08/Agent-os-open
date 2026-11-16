// @feature FP-T12 前端适配（P-24 更新提示面） | @ci: frontend-test
/**
 * UpdateAvailableNotification 测试：新版本可用 → 通知中心条目（P-24，
 * 不做自动更新——只提示版本与下载地址，不下载不安装）。
 *
 * 行为契约：
 * - updateAvailableToNotificationInput：UpdateInfo → 通知载荷（title「新版本
 *   可用」+ 正文含版本号；downloadUrl 非空时正文含地址）；
 * - useUpdateAvailableNotification：挂载时经 electronAPI.update 拉取一次 +
 *   订阅推送；有新版本 → 通知中心出现条目；同版本/未检查（null）→ 无条目
 *   （负例）；非 Electron 环境（window.electronAPI 缺失，dev 浏览器形态）→
 *   no-op 不抛错。
 *
 * Electron IPC 是本测试唯一 mock 的外部边界（与 PluginHealthNotifications
 * 测试对 notification 读面的 mock 同口径）。
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  resetDismissedNotificationIdsForTest,
  useNotificationStore,
} from '@/stores/notificationStore'
import {
  updateAvailableToNotificationInput,
  useUpdateAvailableNotification,
} from '../UpdateAvailableNotification'
import type { UpdateAvailableInfo } from '../UpdateAvailableNotification'

/** electronAPI.update 子 API 的测试桩（拉取 + 推送双通道） */
function stubUpdateApi(initial: UpdateAvailableInfo | null) {
  const listeners: Array<(info: UpdateAvailableInfo) => void> = []
  const api = {
    getAvailable: vi.fn(async () => initial),
    onAvailable: vi.fn((cb: (info: UpdateAvailableInfo) => void) => {
      listeners.push(cb)
      return () => {
        const i = listeners.indexOf(cb)
        if (i >= 0) listeners.splice(i, 1)
      }
    }),
  }
  window.electronAPI = {
    ...window.electronAPI,
    update: api,
  } as typeof window.electronAPI
  return {
    api,
    emit: (info: UpdateAvailableInfo) => {
      for (const cb of listeners) cb(info)
    },
    cleanup: () => {
      delete (window.electronAPI as { update?: unknown } | undefined)?.update
    },
  }
}

function resetNotificationState(): void {
  resetDismissedNotificationIdsForTest()
  useNotificationStore.setState({
    notifications: [],
    isPanelOpen: false,
    activeBlockingNotification: null,
  })
}

const SAMPLE: UpdateAvailableInfo = {
  latestVersion: '0.3.0',
  downloadUrl: 'https://example.invalid/download',
}

beforeEach(() => {
  resetNotificationState()
})

describe('updateAvailableToNotificationInput 载荷映射', () => {
  it('title=新版本可用；正文含版本号与下载地址', () => {
    const input = updateAvailableToNotificationInput(SAMPLE)
    expect(input.title).toBe('新版本可用')
    expect(input.message).toContain('0.3.0')
    expect(input.message).toContain('https://example.invalid/download')
    expect(input.isBlocking).toBe(false)
  })

  it('downloadUrl 为空串 → 正文不含地址段（无地址不渲染空链接）', () => {
    const input = updateAvailableToNotificationInput({ ...SAMPLE, downloadUrl: '' })
    expect(input.title).toBe('新版本可用')
    expect(input.message).toContain('0.3.0')
    expect(input.message).not.toContain('下载地址：')
  })
})

describe('useUpdateAvailableNotification 通知中心同步', () => {
  it('拉取到落后版本 → 通知中心出现「新版本可用」条目（版本与地址在正文）', async () => {
    const stub = stubUpdateApi(SAMPLE)
    renderHook(() => useUpdateAvailableNotification())
    await waitFor(() => {
      const list = useNotificationStore.getState().notifications
      expect(list.some((n) => n.title === '新版本可用')).toBe(true)
    })
    const entry = useNotificationStore.getState().notifications.find(
      (n) => n.title === '新版本可用',
    )
    expect(entry).toBeDefined()
    expect(entry!.message).toContain('0.3.0')
    expect(entry!.message).toContain('https://example.invalid/download')
    stub.cleanup()
  })

  it('负例：同版本/未检查（null）→ 无任何条目', async () => {
    const stub = stubUpdateApi(null)
    renderHook(() => useUpdateAvailableNotification())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    stub.cleanup()
  })

  it('推送通道：挂载后主进程推送 → 条目出现', async () => {
    const stub = stubUpdateApi(null)
    renderHook(() => useUpdateAvailableNotification())
    await act(async () => {
      await Promise.resolve()
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    act(() => {
      stub.emit(SAMPLE)
    })
    await waitFor(() => {
      expect(
        useNotificationStore.getState().notifications.some((n) => n.title === '新版本可用'),
      ).toBe(true)
    })
    stub.cleanup()
  })

  it('负例：非 Electron 环境（electronAPI.update 缺失）→ no-op 无异常无条目', async () => {
    delete (window.electronAPI as { update?: unknown } | undefined)?.update
    renderHook(() => useUpdateAvailableNotification())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})
