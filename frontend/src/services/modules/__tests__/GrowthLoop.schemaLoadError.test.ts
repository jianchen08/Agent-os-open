/** @feature FP-0.2.四/五 fallback-audit FE项 插件贡献加载失败可见降级 @ci frontend-test */
/**
 * reloadContributionRegistry 失败链路（经 refreshPluginContributions 出口）：
 * 不阻塞主流程（不抛出）+ console.warn 留痕 + 非阻塞通知（60s 节流）——
 * 插件贡献（pages/导航/命令/卡片）整体消失时用户有可见指示。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'

const mocks = vi.hoisted(() => ({
  getSchema: vi.fn(),
}))

vi.mock('@/services/api/schema', () => ({
  getSchema: mocks.getSchema,
}))

vi.mock('@/services/websocket/resync', () => ({
  initResyncOnSchema: vi.fn(),
  disposeResyncOnSchema: vi.fn(),
}))

vi.mock('@/services/dshAdapter', () => ({
  loadDshAdapterContributions: vi.fn().mockResolvedValue(undefined),
}))

import { refreshPluginContributions } from '@/services/modules/GrowthLoop'
import { useNotificationStore } from '@/stores/notificationStore'

/** 安装通知 spy，返回捕获函数 */
function spyOnAddNotification(): ReturnType<typeof vi.fn> {
  const spy = vi.fn()
  useNotificationStore.setState({ addNotification: spy as never })
  return spy
}

describe('GrowthLoop schema 加载失败可见降级（FE11）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useNotificationStore.getState().clearAll()
  })

  it('getSchema 失败：不抛出 + warn 留痕 + 发一条非阻塞通知', async () => {
    const addNotificationSpy = spyOnAddNotification()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      mocks.getSchema.mockRejectedValue(new Error('schema endpoint down'))

      // 不阻塞主流程：不抛出
      await expect(refreshPluginContributions()).resolves.toBeUndefined()

      // 真实 logger 会把 (message, error) 插值成单条字符串，断言消息本体
      expect(warnSpy).toHaveBeenCalledWith(
        expect.any(String),
        expect.stringContaining('ContributionRegistry 加载失败'),
      )
      expect(addNotificationSpy).toHaveBeenCalledTimes(1)
      const notification = addNotificationSpy.mock.calls[0][0]
      expect(notification.title).toBe('插件贡献加载失败')
      expect(notification.isBlocking).toBe(false)
      expect(notification.message).toContain('schema endpoint down')
    } finally {
      warnSpy.mockRestore()
    }
  }, 30_000)

  it('连续失败 60s 节流只发一条通知；窗口外再发', async () => {
    vi.useFakeTimers()
    try {
      // 拨到远未来：既覆盖上一用例留下的节流时间戳，也获得可控时钟
      vi.setSystemTime(new Date('2030-01-01T00:00:00Z'))
      const addNotificationSpy = spyOnAddNotification()
      mocks.getSchema.mockRejectedValue(new Error('down again'))

      await refreshPluginContributions()
      await refreshPluginContributions()
      expect(addNotificationSpy).toHaveBeenCalledTimes(1)

      // 超过节流窗口后的失败再次通知
      vi.setSystemTime(new Date('2030-01-01T00:01:01Z'))
      await refreshPluginContributions()
      expect(addNotificationSpy).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  }, 30_000)
})
