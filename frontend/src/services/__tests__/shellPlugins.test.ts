/** @feature FP-0.2.四 壳插件类型化访问器（shellPlugins 直测） | @ci: frontend-test */
/**
 * shellPlugins 访问器与通知权限统一查询直测：KernelHost 桩在场返回类型化
 * 引用；壳内 checkPermissions 异常回落 unsupported；浏览器路径 Notification
 * 权限映射（default→prompt 同义归一）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getShellKernelHost,
  queryNotificationPermission,
} from '@/services/shellPlugins'

const win = window as unknown as { Capacitor?: { Plugins?: Record<string, unknown> } }

beforeEach(() => {
  win.Capacitor = undefined
})

afterEach(() => {
  win.Capacitor = undefined
  vi.unstubAllGlobals()
})

describe('getShellKernelHost 壳插件访问器', () => {
  it('KernelHost 桩在场 → 返回类型化引用；不在场 → null', () => {
    const stub = { status: vi.fn() }
    win.Capacitor = { Plugins: { KernelHost: stub } }
    expect(getShellKernelHost()).toBe(stub)
    win.Capacitor = { Plugins: {} }
    expect(getShellKernelHost()).toBeNull()
  })
})

describe('queryNotificationPermission 权限统一查询', () => {
  it('壳内 checkPermissions 异常 → unsupported（不抛错）', async () => {
    win.Capacitor = {
      Plugins: {
        LocalNotifications: {
          checkPermissions: vi.fn().mockRejectedValue(new Error('bridge gone')),
        },
      },
    }
    await expect(queryNotificationPermission()).resolves.toBe('unsupported')
  })

  it('浏览器回落 Web Notifications API：default 归一为 prompt，granted 透传', async () => {
    vi.stubGlobal('Notification', { permission: 'default' })
    await expect(queryNotificationPermission()).resolves.toBe('prompt')
    vi.stubGlobal('Notification', { permission: 'granted' })
    await expect(queryNotificationPermission()).resolves.toBe('granted')
  })
})
