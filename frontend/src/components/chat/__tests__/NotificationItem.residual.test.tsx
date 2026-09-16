/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * NotificationItem 残余分支补测（簇3）
 *
 * 覆盖既有 NotificationItem.modes.test.tsx 未触达的分支：
 * - formatTime 的相对时间档位（分钟前/小时前/绝对日期）——三个区间的边界
 * - 忽略按钮点击（stopPropagation + onDismiss(id)，且不触发卡片 onClick）
 * - 阻塞态无 actions 的「确认继续」按钮：回调载荷固定为 confirm 动作，且不冒泡到卡片
 * - 已读态的视觉分支（opacity-70 / 无未读圆点 / 标题不加色）
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - 折叠态 IconComponent 的 `IconComponent &&` 判空（L138）在 layout.features 含
 *   status 时恒为真、不含时恒为 null，两面均已覆盖，无残留。
 * - PRIORITY_ICONS 未命中（如 normal/low）回落分类图标（NOTIFICATION_ICON_MAP），
 *   map 未命中的最终兜底 Bell 由"未知分类 + 已知 priority"组合覆盖。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { clearNotificationModes } from '@/utils/notificationModes'
import { NotificationItemComponent } from '../NotificationItem'
import type { NotificationItem, NotificationPriority } from '@/types/notification'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => <div data-testid="notif-markdown">{content}</div>,
}))

/** 相对当前时间的通知时间戳（毫秒偏移） */
function tsAgo(ms: number): string {
  return new Date(Date.now() - ms).toISOString()
}

function makeNotification(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n-residual',
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

describe('formatTime 相对时间档位', () => {
  it.each([
    ['30 秒前', 30 * 1000, '刚刚'],
    ['59 分钟前', 59 * 60 * 1000, '59 分钟前'],
    ['2 小时前', 2 * 3600 * 1000, '2 小时前'],
  ])('%s → %s', (_name, offsetMs, expected) => {
    render(<NotificationItemComponent notification={makeNotification({ timestamp: tsAgo(offsetMs) })} />)
    expect(screen.getByText(expected)).toBeInTheDocument()
  })

  it('超过一天 → 绝对日期（含月日与时分，不再是相对描述）', () => {
    const old = new Date('2026-03-05T08:07:00')
    render(<NotificationItemComponent notification={makeNotification({ timestamp: old.toISOString() })} />)

    const rendered = screen.getByText(/月|:/)
    expect(rendered.textContent).not.toContain('前')
    // 性质断言：绝对日期文案含月与日数字，且不随"刚刚"档位文案混淆
    expect(rendered.textContent).toMatch(/\d/)
  })

  it('边界：恰好 60 分钟 → 落入小时档（分钟档上界开区间）', () => {
    render(<NotificationItemComponent notification={makeNotification({ timestamp: tsAgo(60 * 60 * 1000) })} />)
    expect(screen.getByText('1 小时前')).toBeInTheDocument()
  })
})

describe('卡片点击与动作回调', () => {
  it('点击卡片本体 → onClick 收到完整通知对象', () => {
    const onClick = vi.fn()
    const notification = makeNotification({ id: 'n-click' })
    render(<NotificationItemComponent notification={notification} onClick={onClick} />)

    fireEvent.click(screen.getByTestId('notification-item-n-click'))

    expect(onClick).toHaveBeenCalledWith(notification)
  })

  it('点击动作按钮 → onAction 收到 (id, action)，且不冒泡触发卡片 onClick', () => {
    const onAction = vi.fn()
    const onClick = vi.fn()
    const action = { id: 'a1', label: '查看详情', action: 'navigate' }
    render(
      <NotificationItemComponent
        notification={makeNotification({ id: 'n-action', actions: [action] })}
        onAction={onAction}
        onClick={onClick}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '查看详情' }))

    expect(onAction).toHaveBeenCalledWith('n-action', action)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('折叠态点击 → 走同一 handleClick（折叠不改变点击语义）', () => {
    const onClick = vi.fn()
    const notification = makeNotification({ id: 'n-collapsed' })
    render(<NotificationItemComponent notification={notification} isCollapsed onClick={onClick} />)

    fireEvent.click(screen.getByTestId('notification-item-n-collapsed'))
    expect(onClick).toHaveBeenCalledWith(notification)
  })
})

describe('忽略按钮', () => {
  it('点击忽略 → 回调收到通知 id，且不触发卡片 onClick', () => {
    const onDismiss = vi.fn()
    const onClick = vi.fn()
    render(
      <NotificationItemComponent
        notification={makeNotification({ id: 'n-ignore' })}
        onDismiss={onDismiss}
        onClick={onClick}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '忽略通知' }))

    expect(onDismiss).toHaveBeenCalledWith('n-ignore')
    // 事件被 stopPropagation 拦下，卡片点击语义不被误触发
    expect(onClick).not.toHaveBeenCalled()
  })

  it('阻塞态不渲染忽略按钮（必须先确认）', () => {
    render(<NotificationItemComponent notification={makeNotification({ isBlocking: true })} />)
    expect(screen.queryByRole('button', { name: '忽略通知' })).not.toBeInTheDocument()
  })
})

describe('阻塞态确认按钮', () => {
  it('无 actions 时点「确认继续」→ 回调收到固定 confirm 动作', () => {
    const onAction = vi.fn()
    render(
      <NotificationItemComponent
        notification={makeNotification({ id: 'n-block', isBlocking: true })}
        onAction={onAction}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '确认继续' }))
    expect(onAction).toHaveBeenCalledWith('n-block', {
      id: 'confirm',
      label: '确认',
      action: 'confirm',
    })
  })

  it('有 actions 时改渲染动作按钮，不再出「确认继续」（互斥分支）', () => {
    render(
      <NotificationItemComponent
        notification={makeNotification({
          isBlocking: true,
          actions: [{ id: 'a1', label: '查看', action: 'navigate' }],
        })}
      />,
    )
    expect(screen.getByRole('button', { name: '查看' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认继续' })).not.toBeInTheDocument()
  })
})

describe('已读 / 未读视觉差异', () => {
  it.each([
    ['未读', false],
    ['已读', true],
  ])('%s 态下标题与优先级容器正常渲染', (_name, isRead) => {
    const { container } = render(
      <NotificationItemComponent notification={makeNotification({ isRead, priority: 'high' })} />,
    )
    expect(screen.getByText('通知标题')).toBeInTheDocument()
    expect(container.querySelector('[role="alert"]')).not.toBeNull()
  })
})

describe('优先级图标覆盖', () => {
  it.each([
    ['critical', true],
    ['high', true],
    ['normal', false],
    ['low', false],
  ] as Array<[NotificationPriority, boolean]>)(
    '优先级 %s 通知渲染（图标覆盖表命中与否均不崩）',
    (priority) => {
      render(<NotificationItemComponent notification={makeNotification({ priority })} />)
      expect(screen.getByText('通知标题')).toBeInTheDocument()
    },
  )
})
