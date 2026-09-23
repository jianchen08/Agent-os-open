// @feature: FP-T12 前端适配(OBS-R259-1 通知点击路由跳转) | @ci: frontend-test
/**
 * NotificationCenter 通知点击路由测试
 *
 * 契约（OBS-R259-1）：通知面板单条通知点击按 payload 路由——
 * - 含 task_id → 打开/激活任务管理签并携带 focusTaskId 定位该任务；
 * - 含 thread/session → 切换到该会话（会话已不在列表 → 维持现状不跳转）；
 * - 都无 → 维持现状（面板保持打开、零导航）；
 * - sourceId 命中 pending 交互时交互面板优先（既有行为不回退）。
 *
 * mock 约定：仅 mock 外部边界（会话列表读数、会话切换 store、markdown 渲染）；
 * 任务管理签打开链路走真实 workspacePanelOpener + layoutModeStore，断言 store 终态。
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { resolveNotificationRoute, useNotificationStore  } from '@/stores/notificationStore'
import { NotificationCenter } from '../NotificationCenter'
import {
  makeNotification,
  makePendingInteraction,
  resetNotificationStores,
} from './helpers/notificationTestUtils'
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

async function openPanelAndClick(item: NotificationItem) {
  useNotificationStore.setState({ notifications: [item] })
  render(<NotificationCenter />)
  await userEvent
    .setup()
    .click(screen.getByTestId('notification-center-trigger'))
  await userEvent.setup().click(screen.getByTestId(`notification-item-${item.id}`))
}

function resetStores(): void {
  resetNotificationStores()
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
}

beforeEach(() => {
  resetStores()
  readSessionsMock.mockReturnValue([])
  setActiveSessionMock.mockClear()
})

describe('resolveNotificationRoute（payload → 路由目标数据面）', () => {
  it('task_id 优先解析为任务目标（与 sessionId 并存时任务胜出）', () => {
    expect(resolveNotificationRoute({ taskId: 'task-9', sessionId: 'th-1' })).toEqual({
      kind: 'task',
      taskId: 'task-9',
    })
  })

  it('仅会话坐标解析为会话目标；都无 → null（维持现状）', () => {
    expect(resolveNotificationRoute({ sessionId: 'th-1' })).toEqual({
      kind: 'session',
      sessionId: 'th-1',
    })
    expect(resolveNotificationRoute({})).toBeNull()
  })
})

describe('NotificationCenter 点击路由（task_id）', () => {
  it('含 task_id：打开并激活任务管理签、携带 focusTaskId、关闭通知面板', async () => {
    await openPanelAndClick(makeNotification({ id: 't1', taskId: 'task-9' }))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-panel-tasks'])
    expect(tabs[0]?.isActive).toBe(true)
    expect(tabs[0]?.props).toMatchObject({ focusTaskId: 'task-9' })
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
  })

  it('任务管理签已存在时仅激活并更新 focusTaskId，不重复开签', async () => {
    useLayoutModeStore.setState({
      workspaceTabs: [
        {
          id: 'ws-panel-tasks',
          title: '任务管理',
          moduleId: '__panel_tasks__',
          component: 'pipeline_manager',
          isActive: false,
          isPinned: false,
        },
      ],
      visitedTabIds: [],
    })
    await openPanelAndClick(makeNotification({ id: 't2', taskId: 'task-10' }))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.isActive).toBe(true)
    expect(tabs[0]?.props).toMatchObject({ focusTaskId: 'task-10' })
  })
})

describe('NotificationCenter 点击路由（thread/session）', () => {
  it('会话在列表中：切换到该会话并关闭面板', async () => {
    readSessionsMock.mockReturnValue([{ id: 'th-1', title: '会话一' }])
    await openPanelAndClick(makeNotification({ id: 's1', sessionId: 'th-1' }))

    expect(setActiveSessionMock).toHaveBeenCalledWith('th-1')
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
  })

  it('会话已不在列表：维持现状（不切换、面板保持打开）', async () => {
    await openPanelAndClick(makeNotification({ id: 's2', sessionId: 'th-gone' }))

    expect(setActiveSessionMock).not.toHaveBeenCalled()
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
  })
})

describe('NotificationCenter 点击路由（无坐标 / 交互优先）', () => {
  it('无 task_id 也无 session：维持现状（零导航、面板保持打开）', async () => {
    await openPanelAndClick(makeNotification({ id: 'p1' }))

    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
    expect(setActiveSessionMock).not.toHaveBeenCalled()
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
  })

  it('sourceId 命中 pending 交互：交互面板优先，不开任务签不切会话', async () => {
    useInteractionStore.setState({
      pendingInteractions: [makePendingInteraction()],
    })
    await openPanelAndClick(
      makeNotification({ id: 'mix1', sourceId: 'req-1', taskId: 'task-9', sessionId: 'th-1' }),
    )

    expect(useInteractionStore.getState().globalOpenRequestId).toBe('req-1')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
    expect(setActiveSessionMock).not.toHaveBeenCalled()
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
  })
})
