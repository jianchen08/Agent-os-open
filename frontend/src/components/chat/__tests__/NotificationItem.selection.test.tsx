/** @feature FP-T12 None | @ci: frontend-test */
// @feature: 通知内容区文本选区不应触发条目导航（拖选文字不关通知栏） | @ci: frontend-test
/**
 * 通知内容区「拖选文字即关闭面板」回归测试
 *
 * 缺陷：浏览器在鼠标拖选文字后的 mouseup 仍会派发 click（目标为 mousedown/
 * mouseup 的最近公共祖先），该 click 冒泡到通知条目根节点 → 被当成「点击
 * 通知」→ 触发条目导航（sourceId 命中待处理交互时直接关面板）。
 *
 * 覆盖：
 * - 拖选产生非折叠选区 → 条目不触发导航回调
 * - 指针位移超阈值（拖拽）且无选区 → 条目不触发导航回调
 * - 普通点击（无选区、无位移）→ 条目仍触发导航回调（不回归）
 * - 内容区自带交互元素（复制按钮）点击 → 不冒泡成条目级动作
 * - 面板级：拖选通知正文后面板保持打开、不跳转交互浮层
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { NotificationCenter } from '../NotificationCenter'
import { NotificationItemComponent } from '../NotificationItem'
import {
  makeNotification,
  makePendingInteraction,
  resetNotificationStores,
} from './helpers/notificationTestUtils'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="notif-markdown">
      {content}
      <button type="button" data-testid="notif-copy-button">
        复制
      </button>
    </div>
  ),
}))

/** 在目标元素内容上建立非折叠选区（等价于鼠标拖选） */
function selectText(element: Element): void {
  const range = document.createRange()
  range.selectNodeContents(element)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

beforeEach(() => {
  resetNotificationStores()
  window.getSelection()?.removeAllRanges()
})

describe('NotificationItem 文本选区不触发条目导航', () => {
  it('拖选正文产生选区后派发的 click：不触发导航回调', () => {
    const onClick = vi.fn()
    render(
      <NotificationItemComponent
        notification={makeNotification({ message: '可拖选的通知正文' })}
        onClick={onClick}
      />,
    )
    const message = screen.getByTestId('notif-markdown')
    selectText(message)

    fireEvent.click(message)

    expect(onClick).not.toHaveBeenCalled()
  })

  it('指针位移超阈值（拖拽选字）且无选区：不触发导航回调', () => {
    const onClick = vi.fn()
    render(
      <NotificationItemComponent
        notification={makeNotification({ message: '可拖拽的通知正文' })}
        onClick={onClick}
      />,
    )
    const item = screen.getByTestId('notification-item-n-1')

    fireEvent.mouseDown(item, { clientX: 100, clientY: 100 })
    fireEvent.click(item, { clientX: 140, clientY: 100 })

    expect(onClick).not.toHaveBeenCalled()
  })

  it('普通点击（无选区、无位移）：仍触发导航回调', () => {
    const onClick = vi.fn()
    render(
      <NotificationItemComponent
        notification={makeNotification({ message: '普通通知正文' })}
        onClick={onClick}
      />,
    )
    const item = screen.getByTestId('notification-item-n-1')

    fireEvent.mouseDown(item, { clientX: 100, clientY: 100 })
    fireEvent.click(item, { clientX: 101, clientY: 101 })

    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('内容区复制按钮点击：不冒泡成条目级导航', () => {
    const onClick = vi.fn()
    render(
      <NotificationItemComponent
        notification={makeNotification({ message: '带代码块的通知' })}
        onClick={onClick}
      />,
    )

    fireEvent.click(screen.getByTestId('notif-copy-button'))

    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('NotificationCenter 拖选文字不关面板', () => {
  it('拖选通知正文：面板保持打开、不跳交互浮层', () => {
    useInteractionStore.setState({
      pendingInteractions: [makePendingInteraction()],
    })
    // 带 sourceId 的通知：点击会命中待处理交互 → 打开交互浮层并关面板
    useNotificationStore.setState({
      notifications: [makeNotification({ id: 'sel-1', message: '可拖选正文', sourceId: 'req-1' })],
    })
    render(<NotificationCenter />)

    // 打开面板（单列时间线，条目直接可见）
    fireEvent.click(screen.getByTestId('notification-center-trigger'))

    const message = screen.getByTestId('notif-markdown')
    selectText(message)
    fireEvent.click(message)

    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
    expect(useInteractionStore.getState().globalOpenRequestId).toBeNull()
  })
})
