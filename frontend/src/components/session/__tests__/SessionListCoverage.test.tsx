/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * SessionList 覆盖缺口补测（与 SessionList.test.tsx 互补，不重复）
 *
 * 覆盖契约（Radix Dropdown/Dialog 均用真实用户事件序列触发懒挂载）：
 * - 菜单「删除」→ 删除确认对话框出现，标题回显目标会话；「取消」关闭且不回调
 * - 「确认删除」调用 onDeleteSession(目标 id) 并关闭对话框（完成后）
 * - 删除回调 reject 不阻塞对话框收尾（错误由 store 层处理，isDeleting 复位）
 * - 目标会话不在列表时对话框标题回退「此会话」
 * - 置顶分组与普通分组标题、置顶徽标
 * - 时间元信息：同日显示 HH:MM，跨日显示 M/D，非法时间显示空、缺字段整行为空
 * - onResetMessages 存在时菜单显示「重置消息」，点击回调收到会话 id
 *
 * 说明：`formatSessionMeta` 的两条防御分支均有用例覆盖——非法字符串由
 * `Number.isNaN(getTime())` 拦下，宿主对象（Symbol）经 `new Date` 抛错后由
 * catch 兜底返回空串（会话数据来自后端 JSON，正常不会出现 Symbol，此处按
 * 「属性可被任意赋值」的组件契约补测防御分支）。
 *
 * 测试策略：真实组件 + 真实 Radix 组件（用户事件序列）与假定时器无关；
 * 回调用 spy 断言参数（用户可观察行为）。
 */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SessionList } from '../SessionList'
import type { Session } from '@/types/models'

function createSession(overrides: Partial<Session> = {}): Session {
  return {
    id: `session-${Math.random().toString(36).slice(2, 9)}`,
    title: '测试会话',
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T12:00:00Z',
    messageCount: 5,
    starred: false,
    pinned: false,
    ...overrides,
  } as Session
}

const callbacks = {
  onSessionClick: vi.fn(),
  onDeleteSession: vi.fn().mockResolvedValue(undefined),
  onEditSession: vi.fn(),
  onCopySession: vi.fn(),
  onStarSession: vi.fn(),
  onPinSession: vi.fn(),
}

/** Radix 菜单需完整指针序列才打开 */
function openMenu(trigger: HTMLElement) {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

function renderList(sessions: Session[], extra: Record<string, unknown> = {}) {
  return render(
    <SessionList
      sessions={sessions}
      activeSessionId={null}
      deletingSessionIds={new Set()}
      {...callbacks}
      {...extra}
    />,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionList — 删除确认流程', () => {
  it('菜单「删除」打开确认对话框并回显会话标题，「取消」关闭且不调用删除', async () => {
    const session = createSession({ title: '待删会话' })
    renderList([session])

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText('删除'))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: '确认删除' })).toBeInTheDocument()
    expect(dialog.textContent).toContain('待删会话')

    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    expect(callbacks.onDeleteSession).not.toHaveBeenCalled()
  })

  it('「确认删除」以目标会话 id 调用回调并关闭对话框', async () => {
    const session = createSession({ id: 'target-1', title: '目标' })
    renderList([session])

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText('删除'))
    fireEvent.click(await screen.findByRole('button', { name: /确认删除/ }))

    await waitFor(() => {
      expect(callbacks.onDeleteSession).toHaveBeenCalledWith('target-1')
    })
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
  })

  it('删除回调 reject 时对话框仍收尾（错误由 store 层处理）', async () => {
    callbacks.onDeleteSession.mockRejectedValueOnce(new Error('boom'))
    renderList([createSession({ id: 'bad', title: '失败' })])

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText('删除'))
    fireEvent.click(await screen.findByRole('button', { name: /确认删除/ }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    expect(callbacks.onDeleteSession).toHaveBeenCalledWith('bad')
  })

  it('Escape 关闭删除确认对话框（onOpenChange 关闭路径，不执行删除）', async () => {
    renderList([createSession({ id: 'esc', title: 'ESC 关闭' })])

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText('删除'))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    expect(callbacks.onDeleteSession).not.toHaveBeenCalled()
  })

  it('目标会话从列表消失后标题回退「此会话」', async () => {
    const session = createSession({ id: 'gone', title: '即将消失' })
    const view = renderList([session])

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText('删除'))
    await screen.findByRole('dialog')

    // 列表数据被上层刷新移除该会话（对话框仍开）
    view.rerender(
      <SessionList
        sessions={[]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('此会话')
  })
})

describe('SessionList — 分组与元信息', () => {
  it('置顶会话独立分组并带置顶徽标，普通会话在全部会话组', () => {
    renderList([
      createSession({ id: 'p1', title: '置顶的', pinned: true }),
      createSession({ id: 'n1', title: '普通的', pinned: false }),
    ])

    expect(screen.getByText('已置顶')).toBeInTheDocument()
    expect(screen.getByText('全部会话')).toBeInTheDocument()
    const pinnedGroup = document.querySelector('[data-group="pinned"]') as HTMLElement
    expect(within(pinnedGroup).getByText('置顶的')).toBeInTheDocument()
    expect(within(pinnedGroup).getByTestId('pin-icon')).toBeInTheDocument()

    const normalGroup = document.querySelector('[data-group="normal"]') as HTMLElement
    expect(within(normalGroup).queryByText('置顶的')).toBeNull()
  })

  it('无置顶会话时不渲染置顶分组', () => {
    renderList([createSession({ title: '仅普通' })])
    expect(screen.queryByText('已置顶')).toBeNull()
    expect(screen.getByText('全部会话')).toBeInTheDocument()
  })

  it.each([
    ['2026-01-01T08:05:00', '08:05'],
    ['2026-01-05T00:00:00', '1/5'],
  ] as const)('元信息时间 %s 渲染为 %s', (updatedAt, expected) => {
    // 固定「现在」为 2026-01-01 正午：同日显示 HH:MM，跨日显示 M/D
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T12:00:00'))
    renderList([createSession({ updatedAt })])
    expect(screen.getByText(expected)).toBeInTheDocument()
    vi.useRealTimers()
  })

  it.each([
    ['invalid-date'],
    [''],
  ] as const)('时间字段非法（%s）时不渲染时间元信息', (updatedAt) => {
    renderList([createSession({ updatedAt, createdAt: updatedAt })])
    const item = screen.getByRole('button', { name: /会话: 测试会话/ })
    expect(within(item).queryByText(/\d{1,2}:\d{2}|\d{1,2}\/\d{1,2}/)).toBeNull()
  })

  it('时间字段为无法转数值的宿主对象（Symbol）时静默显示空元信息（catch 兜底）', () => {
    renderList([
      createSession({ title: '坏时间', updatedAt: Symbol('bad') as unknown as string }),
    ])
    const item = screen.getByRole('button', { name: /会话: 坏时间/ })
    // new Date(Symbol) 抛 TypeError → catch 返回 ''，不崩溃
    expect(within(item).queryByText(/\d{1,2}:\d{2}|\d{1,2}\/\d{1,2}/)).toBeNull()
  })

  it('同组多条会话按更新时间倒序（最新在前）', () => {
    renderList([
      createSession({ id: 'older', title: '较旧', updatedAt: '2026-01-01T00:00:00Z' }),
      createSession({ id: 'newer', title: '较新', updatedAt: '2026-06-01T00:00:00Z' }),
    ])
    const items = document.querySelectorAll('[data-group="normal"] [role="button"]')
    expect(items[0].textContent).toContain('较新')
    expect(items[1].textContent).toContain('较旧')
  })

  it('正在删除的会话显示加载态且不渲染操作菜单', () => {
    const session = createSession({ id: 'deleting', title: '删除中' })
    renderList([session], { deletingSessionIds: new Set(['deleting']) })
    expect(screen.queryByRole('button', { name: '更多操作' })).toBeNull()
    expect(screen.getByRole('button', { name: /会话: 删除中/ })).toHaveClass('pointer-events-none')
  })

  it('工作空间徽标显示目录末段与隔离图标', () => {
    renderList([
      createSession({
        title: '带工作空间',
        workspace: 'D:\\workspaces\\task-42',
        isolationMode: 'isolated',
      }),
    ])
    expect(screen.getByTitle(/工作空间: D:\\workspaces\\task-42/).textContent).toContain('task-42')
    expect(screen.getByTitle(/工作空间: D:\\workspaces\\task-42/).textContent).toContain('🛡️')
  })
})

describe('SessionList — 菜单回调', () => {
  it('提供 onResetMessages 时菜单含「重置消息」，点击回调收到会话 id', async () => {
    const onResetMessages = vi.fn()
    renderList([createSession({ id: 'r1', title: '重置目标' })], { onResetMessages })

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText('重置消息'))
    expect(onResetMessages).toHaveBeenCalledWith('r1')
  })

  it('未提供 onResetMessages 时菜单不含「重置消息」', async () => {
    renderList([createSession()])
    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    await screen.findByText('编辑会话')
    expect(screen.queryByText('重置消息')).toBeNull()
  })

  it.each([
    ['编辑会话', 'onEditSession'],
    ['复制', 'onCopySession'],
    ['星标', 'onStarSession'],
    ['置顶会话', 'onPinSession'],
  ] as const)('菜单「%s」触发对应回调', async (label, cbName) => {
    const session = createSession({ id: 'c1' })
    renderList([session])

    await act(async () => {
      openMenu(screen.getAllByRole('button', { name: '更多操作' })[0])
    })
    fireEvent.click(await screen.findByText(label))
    if (cbName === 'onEditSession' || cbName === 'onCopySession') {
      expect(callbacks[cbName]).toHaveBeenCalledWith(expect.objectContaining({ id: 'c1' }))
    } else {
      expect(callbacks[cbName]).toHaveBeenCalledWith('c1')
    }
  })

  it('星标按钮直接切换（不经菜单）且不触发会话点击', () => {
    renderList([createSession({ id: 's1', title: '星标目标' })])
    fireEvent.click(screen.getByTestId('star-button'))
    expect(callbacks.onStarSession).toHaveBeenCalledWith('s1')
    expect(callbacks.onSessionClick).not.toHaveBeenCalled()
  })

  it('点击列表项触发会话切换', () => {
    renderList([createSession({ id: 'clicked', title: '点我' })])
    fireEvent.click(screen.getByRole('button', { name: /会话: 点我/ }))
    expect(callbacks.onSessionClick).toHaveBeenCalledWith('clicked')
  })
})
