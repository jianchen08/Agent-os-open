// @feature FP-T12 前端适配（P-26 失败通知卡一键重试） | @ci frontend-test
/**
 * NotificationCenter「重新发送」动作分发测试（P-26）：
 * - 点击重试动作 → 切换到 payload.retrySessionId 对应会话 + 关闭面板
 *   （复用条目点击的既有会话路由：会话在列表中才切换）
 * - 会话已不在列表 → 不切换、面板保持打开（守卫负例）
 * - 动作点击不消除条目本身（layoutAlert 动作先例同语义）
 *
 * mock 约定与 navigate 测试同款：仅 mock 外部边界（会话列表读数、会话切换
 * store、markdown 渲染），断言 store 终态。
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { NotificationCenter } from '../NotificationCenter'
import { makeNotification, resetNotificationStores } from './helpers/notificationTestUtils'
import type { NotificationItem } from '@/types/notification'

const readSessionsMock = vi.hoisted(() => vi.fn(() => []))
const setActiveSessionMock = vi.hoisted(() => vi.fn(() => Promise.resolve()))

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="notif-markdown">{content}</div>
  ),
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: readSessionsMock,
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => ({ setActiveSession: setActiveSessionMock }) },
}))

function makeRetryNotification(sessionId: string): NotificationItem {
  return makeNotification({
    id: 'retry-1',
    priority: 'high',
    category: 'error',
    title: '模型调用失败',
    message: '服务暂不可用',
    sessionId,
    actions: [
      {
        id: 'retry-resend',
        label: '重新发送',
        action: 'custom',
        payload: { retrySessionId: sessionId },
      },
    ],
  })
}

async function openPanel(): Promise<userEvent.UserEvent> {
  const user = userEvent.setup()
  render(<NotificationCenter />)
  await user.click(screen.getByTestId('notification-center-trigger'))
  return user
}

beforeEach(() => {
  resetNotificationStores()
  readSessionsMock.mockReturnValue([])
  setActiveSessionMock.mockClear()
})

describe('NotificationCenter 重试动作分发（P-26）', () => {
  it('点击「重新发送」：切换到对应会话并关闭面板', async () => {
    readSessionsMock.mockReturnValue([{ id: 'th-live', title: '会话一' }])
    useNotificationStore.setState({ notifications: [makeRetryNotification('th-live')] })
    const user = await openPanel()

    await user.click(screen.getByRole('button', { name: '重新发送' }))

    expect(setActiveSessionMock).toHaveBeenCalledWith('th-live')
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
  })

  it('会话已不在列表：不切换、面板保持打开（守卫负例）', async () => {
    useNotificationStore.setState({ notifications: [makeRetryNotification('th-gone')] })
    const user = await openPanel()

    await user.click(screen.getByRole('button', { name: '重新发送' }))

    expect(setActiveSessionMock).not.toHaveBeenCalled()
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
  })

  it('动作点击不消除条目（条目保留在面板）', async () => {
    readSessionsMock.mockReturnValue([{ id: 'th-live', title: '会话一' }])
    useNotificationStore.setState({ notifications: [makeRetryNotification('th-live')] })
    const user = await openPanel()

    await user.click(screen.getByRole('button', { name: '重新发送' }))

    expect(useNotificationStore.getState().notifications).toHaveLength(1)
    expect(useNotificationStore.getState().notifications[0].id).toBe('retry-1')
  })
})
