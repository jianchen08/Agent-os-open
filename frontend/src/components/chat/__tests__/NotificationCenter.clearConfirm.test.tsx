// @feature: FP-T12 前端适配(X18-F1/P-137 通知中心清空二次确认) | @ci: frontend-test
/**
 * X18-F1（P-137）：通知中心「清空」二次确认契约
 *
 * 修复前：清空按钮无确认直接抹掉全部通知且不可恢复。本文件锁定：
 * - 点清空先出 DOM 可见确认层（共享 ConfirmDialog——原生 confirm 在自动化
 *   浏览器被静默 auto-dismiss，BUG-23），未确认不清；
 * - 取消：不清，确认层关闭，条目保留；
 * - 确认：store 清空且持久化摘要同步清（X14-F1 persist 面交互）——持久化
 *   notifications 为空、被清 id 进 dismissedIds（重放不回魂）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  resetDismissedNotificationIdsForTest,
  useNotificationStore,
} from '@/stores/notificationStore'
import { userScopedKey } from '@/utils/userScopedStorage'
import { NotificationCenter } from '../NotificationCenter'
import { makeNotification, resetNotificationStores } from './helpers/notificationTestUtils'

/** 读持久化信封里的 state 面（persist JSON：{ state, version }） */
function persistedState(): {
  notifications?: Array<Record<string, unknown>>
  dismissedIds?: string[]
} {
  const raw = localStorage.getItem(userScopedKey('notification-storage'))
  expect(raw).not.toBeNull()
  return (JSON.parse(raw as string) as { state: Record<string, unknown> }).state as {
    notifications?: Array<Record<string, unknown>>
    dismissedIds?: string[]
  }
}

/** 面板已开 + 两条未读通知的公共现场 */
async function renderOpenPanelWithTwoNotifications(user: ReturnType<typeof userEvent.setup>) {
  useNotificationStore.setState({
    notifications: [
      makeNotification({ id: 'c-1', title: '通知甲' }),
      makeNotification({ id: 'c-2', title: '通知乙' }),
    ],
    isPanelOpen: true,
  })
  render(<NotificationCenter />)
  await user.click(screen.getByRole('button', { name: '清空' }))
  expect(screen.getByRole('dialog', { name: '确认操作' })).toBeInTheDocument()
}

beforeEach(() => {
  localStorage.clear()
  resetNotificationStores()
  resetDismissedNotificationIdsForTest()
})

describe('X18-F1 通知中心清空二次确认', () => {
  it('点击清空先出确认层：未确认不清（条目与未读数原样）', async () => {
    const user = userEvent.setup()
    useNotificationStore.setState({
      notifications: [
        makeNotification({ id: 'c-1', title: '通知甲' }),
        makeNotification({ id: 'c-2', title: '通知乙' }),
      ],
      isPanelOpen: true,
    })
    render(<NotificationCenter />)

    await user.click(screen.getByRole('button', { name: '清空' }))

    // DOM 可见确认层出现（原生 confirm 在自动化环境被静默 auto-dismiss，BUG-23）
    expect(screen.getByRole('dialog', { name: '确认操作' })).toBeInTheDocument()
    // 未确认：store 与未读数原样
    expect(useNotificationStore.getState().notifications).toHaveLength(2)
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心 (2 条未读)',
    )
  })

  it('取消：不清空，确认层关闭，条目保留', async () => {
    const user = userEvent.setup()
    await renderOpenPanelWithTwoNotifications(user)

    await user.click(screen.getByRole('button', { name: '取消' }))

    expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument()
    expect(useNotificationStore.getState().notifications.map((n) => n.id)).toEqual([
      'c-1',
      'c-2',
    ])
  })

  it('确认后清空：store 清空且持久化摘要同步清（dismissed 记忆落盘防重放回魂）', async () => {
    const user = userEvent.setup()
    await renderOpenPanelWithTwoNotifications(user)

    await user.click(screen.getByRole('button', { name: '确认' }))

    await waitFor(() => {
      expect(useNotificationStore.getState().notifications).toHaveLength(0)
    })
    expect(screen.getByText('暂无通知')).toBeInTheDocument()

    // persist 面交互（X14-F1）：持久化条目摘要同步清空；被清 id 进 dismissedIds
    const persisted = persistedState()
    expect(persisted.notifications).toEqual([])
    expect(persisted.dismissedIds).toEqual(expect.arrayContaining(['c-1', 'c-2']))
  })

  it('确认层打开期间点击确认层遮罩等同取消：不清空且面板不关闭', async () => {
    const user = userEvent.setup()
    await renderOpenPanelWithTwoNotifications(user)

    // ConfirmDialog 契约：遮罩点击等同取消（遮罩=对话框根的首个子元素）
    const dialog = screen.getByRole('dialog', { name: '确认操作' })
    await user.click(dialog.firstElementChild as HTMLElement)

    expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument()
    expect(useNotificationStore.getState().notifications).toHaveLength(2)
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
  })
})
