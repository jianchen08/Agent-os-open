/**
 * NotificationItem 声明驱动渲染测试（widget 化批1-C）
 *
 * 覆盖：组件消费 registry——内置默认兜底渲染、未知分类默认兜底不崩、
 * 插件声明新分类按词表渲染、插件覆盖改变渲染、数据形状增强（有 actions
 * 自动补）、progress 特性跟分类声明走（不随数据自动补，现状等价）。
 */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NotificationItemComponent } from '../NotificationItem'
import {
  clearNotificationModes,
  loadNotificationModes,
} from '@/utils/notificationModes'
import type { NotificationItem } from '@/types/notification'

vi.mock('../markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="notif-markdown">{content}</div>
  ),
}))

function makeNotification(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n-1',
    category: 'info',
    title: '通知标题',
    priority: 'normal',
    isBlocking: false,
    isRead: false,
    timestamp: new Date().toISOString(),
    ...overrides,
  }
}

beforeEach(() => clearNotificationModes())
afterEach(() => clearNotificationModes())

describe('NotificationItem 声明驱动渲染', () => {
  it('内置默认件兜底：progress 分类渲染进度条，行为与现状等价', () => {
    render(
      <NotificationItemComponent
        notification={makeNotification({ category: 'progress', progress: 33 })}
      />,
    )
    expect(screen.getByText('33%')).toBeInTheDocument()
  })

  it('progress 特性跟分类声明走：alert 分类带 progress 载荷不出进度条（现状等价）', () => {
    render(
      <NotificationItemComponent
        notification={makeNotification({ category: 'alert', progress: 60 })}
      />,
    )
    expect(screen.queryByText('60%')).not.toBeInTheDocument()
  })

  it('未声明分类：通用默认兜底，正常渲染标题不崩', () => {
    render(
      <NotificationItemComponent
        notification={makeNotification({ category: 'brand_new_category' })}
      />,
    )
    expect(screen.getByText('通知标题')).toBeInTheDocument()
  })

  it('插件声明新分类：按声明词表渲染（progress 特性出进度条）', () => {
    loadNotificationModes([
      { ui: { notification_modes: [{ category: 'deploy', features: ['status', 'message', 'progress'] }] } },
    ])
    render(
      <NotificationItemComponent
        notification={makeNotification({ category: 'deploy', progress: 80, message: '部署中' })}
      />,
    )
    expect(screen.getByText('80%')).toBeInTheDocument()
    expect(screen.getByTestId('notif-markdown')).toBeInTheDocument()
  })

  it('插件覆盖内置默认件：progress 分类去掉 progress 特性 → 进度条不渲染', () => {
    loadNotificationModes([
      { ui: { notification_modes: [{ category: 'progress', features: ['status', 'message'] }] } },
    ])
    render(
      <NotificationItemComponent
        notification={makeNotification({ category: 'progress', progress: 40 })}
      />,
    )
    expect(screen.queryByText('40%')).not.toBeInTheDocument()
  })

  it('数据形状增强：非默认词表分类带 actions 载荷 → 动作按钮渲染', () => {
    render(
      <NotificationItemComponent
        notification={makeNotification({
          category: 'info',
          actions: [{ id: 'a1', label: '查看详情', action: 'navigate' }],
        })}
      />,
    )
    expect(screen.getByRole('button', { name: '查看详情' })).toBeInTheDocument()
  })

  it('阻塞式通知无 actions：确认继续按钮照常渲染（数据驱动，不属词表）', () => {
    render(
      <NotificationItemComponent
        notification={makeNotification({ category: 'info', isBlocking: true })}
      />,
    )
    expect(screen.getByRole('button', { name: '确认继续' })).toBeInTheDocument()
  })
})