/**
 * 通知分类声明注册表测试（widget 化批1-C）
 *
 * 覆盖：声明装载/覆盖内置默认件/未知分类通用兜底/数据形状增强。
 * 对齐 interactionModes 的测试语义：声明驱动 + 默认兜底 + 插件可覆盖。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { NotificationItem } from '@/types/notification'
import {
  clearNotificationModes,
  getNotificationModeDecl,
  loadNotificationModes,
  resolveNotificationLayout,
} from '@/utils/notificationModes'

function makeNotification(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n-1',
    category: 'info',
    title: '通知',
    priority: 'normal',
    isBlocking: false,
    isRead: false,
    timestamp: new Date().toISOString(),
    ...overrides,
  }
}

/** 特性集断言辅助（排序后比对） */
function sortedFeatures(notification: NotificationItem): string[] {
  return [...resolveNotificationLayout(notification).features].sort()
}

beforeEach(() => clearNotificationModes())
afterEach(() => clearNotificationModes())

describe('通知分类声明注册表', () => {
  it('tools[].ui.notification_modes 装载与查询（无 category / 词表外 features 过滤）', () => {
    loadNotificationModes([
      {
        ui: {
          notification_modes: [
            { category: 'deploy', features: ['status', 'progress', 'bogus_feature'], icon: 'loader' },
            { features: ['status'] }, // 无 category → 丢弃
            'garbage',
          ],
        },
      },
      { ui: {} },
      {},
    ])
    const decl = getNotificationModeDecl('deploy')
    expect(decl?.features).toEqual(['status', 'progress'])
    expect(decl?.icon).toBe('loader')
    expect(getNotificationModeDecl('unknown')).toBeUndefined()
  })

  it('内置默认件兜底（五分类词表与现状渲染等价）', () => {
    expect(sortedFeatures(makeNotification({ category: 'progress' }))).toEqual(
      ['message', 'progress', 'status'].sort(),
    )
    expect(sortedFeatures(makeNotification({ category: 'alert' }))).toEqual(
      ['actions', 'message', 'status'].sort(),
    )
    expect(sortedFeatures(makeNotification({ category: 'info' }))).toEqual(
      ['message', 'status'].sort(),
    )
    expect(getNotificationModeDecl('success')?.icon).toBe('check-circle')
    expect(getNotificationModeDecl('error')?.features).toContain('actions')
  })

  it('未知未声明分类：通用兜底（bell 状态图标 + message）', () => {
    const layout = resolveNotificationLayout(makeNotification({ category: 'brand_new_category' }))
    expect(layout.iconKey).toBe('bell')
    expect(layout.features.has('status')).toBe(true)
    expect(layout.features.has('message')).toBe(true)
    expect(layout.features.has('progress')).toBe(false)
  })

  it('声明覆盖内置默认件（progress 去掉 progress 特性 / 换图标键）', () => {
    loadNotificationModes([
      {
        ui: {
          notification_modes: [
            { category: 'progress', features: ['status', 'message'], icon: 'check-circle' },
          ],
        },
      },
    ])
    const layout = resolveNotificationLayout(makeNotification({ category: 'progress', progress: 40 }))
    expect(layout.features.has('progress')).toBe(false)
    expect(layout.iconKey).toBe('check-circle')
  })

  it('数据形状增强：带 message/actions 载荷自动补对应特性（与现状等价）', () => {
    // info 默认词表不含 actions；带 actions 载荷 → 自动补
    const layout = resolveNotificationLayout(
      makeNotification({
        category: 'info',
        actions: [{ id: 'a1', label: '查看', action: 'navigate' }],
      }),
    )
    expect(layout.features.has('actions')).toBe(true)
    // progress 特性不随 progress 数据自动补（现状只有 progress 分类出进度条）
    const alertLayout = resolveNotificationLayout(
      makeNotification({ category: 'alert', progress: 60 }),
    )
    expect(alertLayout.features.has('progress')).toBe(false)
  })

  it('数据形状增强：message 载荷自动补（声明词表不含 message 也补）', () => {
    loadNotificationModes([
      { ui: { notification_modes: [{ category: 'deploy', features: ['status', 'progress'] }] } },
    ])
    const withMsg = resolveNotificationLayout(
      makeNotification({ category: 'deploy', message: '部署中' }),
    )
    const withoutMsg = resolveNotificationLayout(makeNotification({ category: 'deploy' }))
    expect(withMsg.features.has('message')).toBe(true)
    expect(withoutMsg.features.has('message')).toBe(false)
  })
})