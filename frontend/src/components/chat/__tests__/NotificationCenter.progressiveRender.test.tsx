/** @feature X30-F1 P-145 | @ci: frontend-test */
/**
 * NotificationCenter 渐进渲染（P-145）：通知列表无分页全量渲染，200 条 cap
 * 后单面板 DOM 仍可优化。约定：
 * - 初始渲染只出最近 50 条（最新优先，时间倒序不变）
 * - 「显示更早」按钮每点一批 +50 分批展开，连点到按钮消失即全量
 * - 低于初始阈值（30 条）零行为变化：无按钮、全量直渲
 * - 展开态不跨面板开合保留：重开面板回落最近 50
 * 交互零变化约束：分组/已读/清空/点击路由全部不动（既有 panel 测试家族
 * 继续兜住）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { NotificationCenter } from '../NotificationCenter'
import { makeNotification, resetNotificationStores } from './helpers/notificationTestUtils'
import type { NotificationItem } from '@/types/notification'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="notif-markdown">{content}</div>
  ),
}))

/** 造 n 条严格递增时间戳的通知（id n-0 最旧 → n-(n-1) 最新） */
function seedByCount(n: number): NotificationItem[] {
  const base = Date.UTC(2026, 0, 1, 0, 0, 0)
  return Array.from({ length: n }, (_, i) =>
    makeNotification({
      id: `n-${i}`,
      title: `通知 ${i}`,
      timestamp: new Date(base + i * 1000).toISOString(),
    }),
  )
}

function renderedIds(): string[] {
  const list = document.querySelector('[data-testid="notification-list"]')
  return Array.from(
    list?.querySelectorAll('[data-testid^="notification-item-"]') ?? [],
  ).map((el) => el.getAttribute('data-testid')!.replace('notification-item-', ''))
}

async function openPanel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(document.querySelector('[data-testid="notification-center-trigger"]')!)
}

beforeEach(() => {
  resetNotificationStores()
  document.body.style.overflow = ''
  document.documentElement.style.overflow = ''
})

describe('NotificationCenter 渐进渲染（P-145）', () => {
  it('200 条：初始只渲染最近 50 条，按钮提示还有 150 条', async () => {
    const user = userEvent.setup()
    useNotificationStore.setState({ notifications: seedByCount(200) })
    render(<NotificationCenter />)
    await openPanel(user)

    expect(renderedIds()).toHaveLength(50)
    // 时间倒序不变：可见的是最新 50 条（n-150..n-199），最旧的 n-0 不在列
    expect(renderedIds()[0]).toBe('n-199')
    expect(renderedIds()[49]).toBe('n-150')
    expect(renderedIds()).not.toContain('n-0')
    const showOlder = document.querySelector('[data-testid="notification-show-older"]')
    expect(showOlder).not.toBeNull()
    expect(showOlder!.textContent).toContain('还有 150 条')
  })

  it('点击一次展开一批 +50（分批语义，非一次全量）', async () => {
    const user = userEvent.setup()
    useNotificationStore.setState({ notifications: seedByCount(200) })
    render(<NotificationCenter />)
    await openPanel(user)

    await user.click(document.querySelector('[data-testid="notification-show-older"]')!)
    expect(renderedIds()).toHaveLength(100)
    expect(document.querySelector('[data-testid="notification-show-older"]')!.textContent)
      .toContain('还有 100 条')
  })

  it('连点到按钮消失：200 条全量渲染且次序保持最新在前', async () => {
    const user = userEvent.setup()
    useNotificationStore.setState({ notifications: seedByCount(200) })
    render(<NotificationCenter />)
    await openPanel(user)

    let guard = 0
    let older = document.querySelector('[data-testid="notification-show-older"]')
    while (older && guard < 10) {
      await user.click(older)
      older = document.querySelector('[data-testid="notification-show-older"]')
      guard += 1
    }
    expect(older).toBeNull()
    const ids = renderedIds()
    expect(ids).toHaveLength(200)
    expect(ids[0]).toBe('n-199')
    expect(ids[199]).toBe('n-0')
    expect(screen.getByText('通知 0')).toBeInTheDocument()
  })

  it('30 条（低于初始阈值）：无按钮，全量直渲零行为变化', async () => {
    const user = userEvent.setup()
    useNotificationStore.setState({ notifications: seedByCount(30) })
    render(<NotificationCenter />)
    await openPanel(user)

    expect(renderedIds()).toHaveLength(30)
    expect(document.querySelector('[data-testid="notification-show-older"]')).toBeNull()
  })

  it('展开态不跨面板开合保留：重开面板回落最近 50', async () => {
    const user = userEvent.setup()
    useNotificationStore.setState({ notifications: seedByCount(200) })
    render(<NotificationCenter />)
    await openPanel(user)
    await user.click(document.querySelector('[data-testid="notification-show-older"]')!)
    expect(renderedIds()).toHaveLength(100)

    // 关闭（ESC）再重开：回落初始 50
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
    await openPanel(user)
    expect(renderedIds()).toHaveLength(50)
  })
})
