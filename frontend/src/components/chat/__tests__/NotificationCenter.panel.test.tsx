// @feature: FP-T12 NotificationCenter 补测 | @ci: frontend-test
/**
 * NotificationCenter 面板交互测试
 *
 * 覆盖：触发按钮未读 badge、按优先级分组渲染、折叠/展开（含"还有 N 条"）、
 * 点击通知标记已读、sourceId 关联待处理交互时跳转并关面板、动作按钮执行、
 * wheel 冒泡拦截与 body overflow 临时解除/恢复、点击外部与 ESC 关闭、
 * 阻塞式模态框（标题/正文/进度条/动作按钮/确认继续）。
 *
 * mock 约定（沿用 chat 目录既有测试惯例）：MarkdownRenderer 与 ui/dialog
 * 简化渲染；通知/交互两个 Zustand store 用真实实现驱动，测试间重置。
 */
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { NotificationCenter } from '../NotificationCenter'
import {
  makeNotification,
  makePendingInteraction,
  resetNotificationStores,
} from './helpers/notificationTestUtils'
import type { NotificationItem } from '@/types/notification'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="notif-markdown">{content}</div>
  ),
}))

/** 通过 store 公共 API 造通知（走真实排序/阻塞弹层逻辑） */
function seedNotifications(items: NotificationItem[]): void {
  useNotificationStore.setState({ notifications: items })
}

beforeEach(() => {
  resetNotificationStores()
  document.body.style.overflow = ''
  document.documentElement.style.overflow = ''
})

describe('NotificationCenter 触发按钮与未读 badge', () => {
  it('无通知：按钮无未读标注', () => {
    render(<NotificationCenter />)
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )
  })

  it('有未读：badge 显示未读数', () => {
    seedNotifications([
      makeNotification({ id: 'a' }),
      makeNotification({ id: 'b', isRead: true }),
    ])
    render(<NotificationCenter />)
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心 (1 条未读)',
    )
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('未读超过 99：badge 封顶显示 99+（aria-label 保留真实计数）', () => {
    seedNotifications(
      Array.from({ length: 100 }, (_, i) => makeNotification({ id: `n-${i}` })),
    )
    render(<NotificationCenter />)
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心 (100 条未读)',
    )
    expect(screen.getByText('99+')).toBeInTheDocument()
  })
})

describe('NotificationCenter 面板分组渲染', () => {
  it('空通知打开面板：显示暂无通知', async () => {
    const user = userEvent.setup()
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
    expect(screen.getByText('暂无通知')).toBeInTheDocument()
  })

  it('四级优先级各成组：组头计数 + 通知标题可见', async () => {
    const user = userEvent.setup()
    seedNotifications([
      makeNotification({ id: 'c1', priority: 'critical', title: '紧急件' }),
      makeNotification({ id: 'c2', priority: 'critical', title: '紧急件二' }),
      makeNotification({ id: 'h1', priority: 'high', title: '重要件' }),
      makeNotification({ id: 'h2', priority: 'high', title: '重要件二' }),
      makeNotification({ id: 'h3', priority: 'high', title: '重要件三' }),
      makeNotification({ id: 'n1', priority: 'normal', title: '普通件' }),
      makeNotification({ id: 'l1', priority: 'low', title: '低优先件' }),
    ])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    expect(screen.getByText('🔴 紧急')).toBeInTheDocument()
    expect(screen.getByText('(2)')).toBeInTheDocument()
    expect(screen.getByText('🟠 重要')).toBeInTheDocument()
    expect(screen.getByText('(3)')).toBeInTheDocument()
    expect(screen.getByText('🔵 普通')).toBeInTheDocument()
    expect(screen.getByText('⚪ 低优先')).toBeInTheDocument()
    expect(screen.getByText('紧急件')).toBeInTheDocument()
    expect(screen.getByText('重要件三')).toBeInTheDocument()
    expect(screen.getByText('普通件')).toBeInTheDocument()
    expect(screen.getByText('低优先件')).toBeInTheDocument()
  })

  it('组内未读计数徽标按组统计', async () => {
    const user = userEvent.setup()
    seedNotifications([
      makeNotification({ id: 'h1', priority: 'high', title: '未读重要件' }),
      makeNotification({ id: 'h2', priority: 'high', title: '已读重要件', isRead: true }),
    ])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(screen.getByText('1 条未读')).toBeInTheDocument()
  })

  it('全部已读：未读 badge 清零；清空：面板转空态', async () => {
    const user = userEvent.setup()
    seedNotifications([
      makeNotification({ id: 'a' }),
      makeNotification({ id: 'b' }),
    ])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByRole('button', { name: '全部已读' }))
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )

    await user.click(screen.getByRole('button', { name: '清空' }))
    expect(screen.getByText('暂无通知')).toBeInTheDocument()
    expect(screen.queryByText('通知标题')).not.toBeInTheDocument()
  })
})

describe('NotificationCenter 折叠与展开', () => {
  it('默认折叠组只显前 2 条摘要，"还有 N 条"展开全组，组头再折叠', async () => {
    const user = userEvent.setup()
    seedNotifications([
      makeNotification({ id: 'm1', priority: 'normal', title: '普通甲' }),
      makeNotification({ id: 'm2', priority: 'normal', title: '普通乙' }),
      makeNotification({ id: 'm3', priority: 'normal', title: '普通丙' }),
    ])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    // normal 组默认折叠：前 2 条摘要可见，第 3 条不可见
    expect(screen.getByText('普通甲')).toBeInTheDocument()
    expect(screen.getByText('普通乙')).toBeInTheDocument()
    expect(screen.queryByText('普通丙')).not.toBeInTheDocument()
    expect(screen.getByText(/还有 1 条普通通知/)).toBeInTheDocument()

    // 点"还有 N 条"展开全组
    await user.click(screen.getByText(/还有 1 条普通通知/))
    expect(screen.getByText('普通丙')).toBeInTheDocument()

    // 点组头重新折叠
    await user.click(screen.getByTestId('notification-group-normal'))
    expect(screen.queryByText('普通丙')).not.toBeInTheDocument()
  })
})

describe('NotificationCenter 通知点击与交互跳转', () => {
  it('点击未读通知：标记已读（未读 badge 归零）', async () => {
    const user = userEvent.setup()
    seedNotifications([makeNotification({ id: 'u1', title: '未读件' })])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心 (1 条未读)',
    )

    await user.click(screen.getByTestId('notification-item-u1'))
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )
  })

  it('点击已读通知：状态不变', async () => {
    const user = userEvent.setup()
    seedNotifications([makeNotification({ id: 'r1', isRead: true })])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByTestId('notification-item-r1'))
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
  })

  it('sourceId 命中 pending 交互：打开全局交互浮层并关闭面板', async () => {
    const user = userEvent.setup()
    useInteractionStore.setState({
      pendingInteractions: [makePendingInteraction()],
    })
    seedNotifications([makeNotification({ id: 's1', sourceId: 'req-1' })])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByTestId('notification-item-s1'))

    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
    // 全局交互浮层的打开目标由 interactionStore 承载（组件契约：跳转即设置该键）
    expect(useInteractionStore.getState().globalOpenRequestId).toBe('req-1')
  })

  it('sourceId 命中的交互已非 pending：面板保持打开、不跳转', async () => {
    const user = userEvent.setup()
    useInteractionStore.setState({
      pendingInteractions: [makePendingInteraction({ requestId: 'req-2', status: 'responded' })],
    })
    seedNotifications([makeNotification({ id: 's2', sourceId: 'req-2' })])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByTestId('notification-item-s2'))

    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
    expect(useInteractionStore.getState().globalOpenRequestId).toBeNull()
  })

  it('sourceId 无对应交互：面板保持打开、不跳转', async () => {
    const user = userEvent.setup()
    seedNotifications([makeNotification({ id: 's3', sourceId: 'req-missing' })])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByTestId('notification-item-s3'))

    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
    expect(useInteractionStore.getState().globalOpenRequestId).toBeNull()
  })
})

describe('NotificationCenter 通知动作按钮', () => {
  it('dismiss 动作：通知从面板移除', async () => {
    const user = userEvent.setup()
    seedNotifications([
      makeNotification({
        id: 'a1',
        priority: 'high',
        category: 'error',
        title: '可忽略件',
        actions: [{ id: 'rm', label: '忽略它', action: 'dismiss' }],
      }),
    ])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByRole('button', { name: '忽略它' }))
    expect(screen.queryByText('可忽略件')).not.toBeInTheDocument()
    expect(screen.getByText('暂无通知')).toBeInTheDocument()
  })

  it('navigate 动作：通知标记已读、条目保留', async () => {
    const user = userEvent.setup()
    seedNotifications([
      makeNotification({
        id: 'a2',
        priority: 'high',
        category: 'error',
        title: '可跳转件',
        actions: [{ id: 'go', label: '去查看', action: 'navigate' }],
      }),
    ])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    await user.click(screen.getByRole('button', { name: '去查看' }))
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )
    expect(screen.getByText('可跳转件')).toBeInTheDocument()
  })
})

describe('NotificationCenter 滚轮冒泡拦截', () => {
  it('面板内滚轮被拦截不冒泡到 window，面板外滚轮正常传播', async () => {
    const user = userEvent.setup()
    seedNotifications([makeNotification({ id: 'w1' })])
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    const seen: string[] = []
    const probe = () => seen.push('wheel')
    window.addEventListener('wheel', probe)
    try {
      // 面板内：document 捕获层拦截，window 收不到
      fireEvent.wheel(screen.getByTestId('notification-scroll-area'))
      expect(seen).toEqual([])

      // 面板外：正常传播到 window
      fireEvent.wheel(document.body)
      expect(seen).toEqual(['wheel'])
    } finally {
      window.removeEventListener('wheel', probe)
    }
  })
})

describe('NotificationCenter body overflow 临时解除与恢复', () => {
  it('打开面板改为 visible，点击遮罩关闭后恢复原值', async () => {
    const user = userEvent.setup()
    document.body.style.overflow = 'hidden'
    document.documentElement.style.overflow = 'hidden'
    render(<NotificationCenter />)

    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(document.body.style.overflow).toBe('visible')
    expect(document.documentElement.style.overflow).toBe('visible')

    fireEvent.click(screen.getByTestId('notification-overlay'))
    expect(document.body.style.overflow).toBe('hidden')
    expect(document.documentElement.style.overflow).toBe('hidden')
  })

  it('面板打开状态下直接卸载：overflow 恢复原值', async () => {
    const user = userEvent.setup()
    document.body.style.overflow = 'hidden'
    document.documentElement.style.overflow = 'hidden'
    const { unmount } = render(<NotificationCenter />)

    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(document.body.style.overflow).toBe('visible')

    unmount()
    expect(document.body.style.overflow).toBe('hidden')
    expect(document.documentElement.style.overflow).toBe('hidden')
  })
})

describe('NotificationCenter 关闭路径', () => {
  it('点击面板外部（mousedown）关闭', async () => {
    const user = userEvent.setup()
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
  })

  it('面板内 mousedown 与触发按钮上 mousedown 均不关闭', async () => {
    const user = userEvent.setup()
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    fireEvent.mouseDown(screen.getByTestId('notification-scroll-area'))
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()

    fireEvent.mouseDown(screen.getByTestId('notification-center-trigger'))
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
  })

  it('ESC 关闭面板，其他按键不关闭', async () => {
    const user = userEvent.setup()
    render(<NotificationCenter />)
    await user.click(screen.getByTestId('notification-center-trigger'))

    fireEvent.keyDown(document.body, { key: 'Enter' })
    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()

    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.queryByTestId('notification-center-panel')).not.toBeInTheDocument()
  })

  it('hideTrigger：不渲染自带触发按钮', () => {
    render(<NotificationCenter hideTrigger />)
    expect(screen.queryByTestId('notification-center-trigger')).not.toBeInTheDocument()
  })
})

describe('NotificationCenter 阻塞式模态框', () => {
  it('有 message 且带进度：渲染 markdown 正文、进度条与百分比', () => {
    useNotificationStore.setState({
      activeBlockingNotification: makeNotification({
        id: 'b1',
        isBlocking: true,
        category: 'progress',
        title: '等待确认',
        message: '**需要你确认**',
        progress: 42,
      }),
    })
    render(<NotificationCenter />)

    const dialog = screen.getByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(dialog).toHaveTextContent('等待确认')
    expect(screen.getByTestId('notif-markdown')).toHaveTextContent('需要你确认')
    expect(screen.getByText('42%')).toBeInTheDocument()
  })

  it('无 message 无 progress：渲染默认提示且无进度百分比', () => {
    useNotificationStore.setState({
      activeBlockingNotification: makeNotification({
        id: 'b2',
        isBlocking: true,
        category: 'info',
        title: '等待确认',
      }),
    })
    render(<NotificationCenter />)

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('请确认后继续执行')).toBeInTheDocument()
    expect(screen.queryByTestId('notif-markdown')).not.toBeInTheDocument()
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument()
  })

  it('点击动作按钮：执行对应动作，模态框保持打开', async () => {
    const user = userEvent.setup()
    seedNotifications([makeNotification({ id: 'b3', title: '阻塞件' })])
    useNotificationStore.setState({
      activeBlockingNotification: makeNotification({
        id: 'b3',
        isBlocking: true,
        category: 'error',
        title: '阻塞件',
        actions: [{ id: 'go', label: '去处理', action: 'navigate' }],
      }),
    })
    render(<NotificationCenter />)

    await user.click(screen.getByRole('button', { name: '去处理' }))
    // navigate 语义 = 标记已读；模态框保持打开等待后续确认
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )
  })

  it('点击确认继续：模态框关闭且通知标记已读', async () => {
    const user = userEvent.setup()
    seedNotifications([makeNotification({ id: 'b4', title: '阻塞件' })])
    useNotificationStore.setState({
      activeBlockingNotification: makeNotification({
        id: 'b4',
        isBlocking: true,
        title: '阻塞件',
      }),
    })
    render(<NotificationCenter />)

    await user.click(screen.getByRole('button', { name: '确认继续' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )
  })

  it('阻塞模态框不可通过外部点击或 ESC 关闭（仅确认/动作按钮可关）', async () => {
    useNotificationStore.setState({
      activeBlockingNotification: makeNotification({
        id: 'b6',
        isBlocking: true,
        title: '强提醒件',
      }),
    })
    render(<NotificationCenter />)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())

    // Radix 在挂载后的宏任务里才注册 document pointerdown 监听
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })

    // 对话框把外部 pointerdown 的处置推迟到随后的 click（deferPointerDownOutside），
    // 故按真实事件序列派发；preventDefault 后模态框不得被外点关闭
    fireEvent.pointerDown(document.body)
    fireEvent.click(document.body)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('dismiss 类动作按钮：确认后通知从列表移除', async () => {
    const user = userEvent.setup()
    const blocking: NotificationItem = makeNotification({
      id: 'b5',
      isBlocking: true,
      category: 'alert',
      title: '可放弃的阻塞件',
      actions: [{ id: 'drop', label: '放弃', action: 'dismiss' }],
    })
    seedNotifications([blocking])
    useNotificationStore.setState({
      activeBlockingNotification: blocking,
      isPanelOpen: true,
    })
    render(<NotificationCenter />)

    await user.click(screen.getByRole('button', { name: '放弃' }))
    // dismiss 语义：通知移除即解除阻塞（模态框随之关闭）；模态框层不在面板
    // 容器内，mousedown 同时触发面板关闭
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByTestId('notification-center-trigger')).toHaveAttribute(
      'aria-label',
      '通知中心',
    )

    // 重新打开面板确认列表已空
    await user.click(screen.getByTestId('notification-center-trigger'))
    expect(screen.getByText('暂无通知')).toBeInTheDocument()
  })
})

describe('NotificationCenter 面板与 store 状态联动', () => {
  it('store 已开面板时直接渲染面板（侧边栏入口路径）', () => {
    seedNotifications([makeNotification({ id: 'p1', title: '持久重要件', priority: 'high' })])
    useNotificationStore.setState({ isPanelOpen: true })
    render(<NotificationCenter />)

    expect(screen.getByTestId('notification-center-panel')).toBeInTheDocument()
    expect(screen.getByText('持久重要件')).toBeInTheDocument()
  })
})
