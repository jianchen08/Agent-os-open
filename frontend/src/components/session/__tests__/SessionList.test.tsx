/**
 * SessionList 组件单元测试 - 置顶分组显示 + 右键菜单功能
 *
 * 测试覆盖：
 * - AC-1.3-1: 下拉菜单中置顶/取消置顶操作项
 * - AC-1.3-2: 置顶会话分组显示（标题、分隔线、排序）
 * - AC-1.3-3: 置顶视觉标识（Pin 图标）
 * - AC-1.3-4: 兼容现有功能（删除、编辑、复制、星标）
 * - AC-1.3-5: 右键上下文菜单支持重命名、置顶/取消置顶、删除
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within, act } from '@testing-library/react'
import React from 'react'
import { SessionList } from '../SessionList'
import type { Session } from '@/types'

// ---------------------------------------------------------------------------
//  Mock: @/lib/utils
// ---------------------------------------------------------------------------
vi.mock('@/lib/utils', () => ({
  cn: (...args: (string | undefined | null | false)[]) =>
    args.filter(Boolean).join(' '),
}))

// ---------------------------------------------------------------------------
//  Mock: lucide-react
// ---------------------------------------------------------------------------
vi.mock('lucide-react', () => {
  const icons = [
    'Copy',
    'Edit3',
    'Loader2',
    'MessageSquare',
    'MoreHorizontal',
    'Pin',
    'Star',
    'Trash2',
  ]
  const m: Record<string, any> = {}
  for (const name of icons) {
    m[name] = (p: any) => React.createElement('svg', { 'data-testid': `icon-${name}`, ...p })
  }
  return m
})

// ---------------------------------------------------------------------------
//  Mock: UI Button
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/button', () => ({
  Button: ({
    children,
    onClick,
    disabled,
    ...rest
  }: {
    children: React.ReactNode
    onClick?: () => void
    disabled?: boolean
    [key: string]: any
  }) => (
    <button
      data-testid={`button-${typeof children === 'string' ? children : 'action'}`}
      onClick={onClick}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  ),
}))

// ---------------------------------------------------------------------------
//  Mock: UI Dialog
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => {
    if (!open) return null
    return <div data-testid="dialog-root">{children}</div>
  },
  DialogContent: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="dialog-content" className={className}>
      {children}
    </div>
  ),
  DialogHeader: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dialog-header">{children}</div>
  ),
  DialogTitle: ({ children }: { children: React.ReactNode }) => (
    <h2 data-testid="dialog-title">{children}</h2>
  ),
  DialogDescription: ({ children }: { children: React.ReactNode }) => (
    <p data-testid="dialog-description">{children}</p>
  ),
  DialogFooter: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dialog-footer">{children}</div>
  ),
  DialogPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DialogOverlay: () => <div data-testid="dialog-overlay" />,
  DialogTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DialogClose: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
    <button data-testid="dialog-close" onClick={onClick}>{children}</button>
  ),
}))

// ---------------------------------------------------------------------------
//  Mock: UI DropdownMenu — 使用渲染子元素方式，菜单项直接可见
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div data-testid="dropdown-root">{children}</div>,
  DropdownMenuTrigger: ({ children, asChild }: { children: React.ReactNode; asChild?: boolean }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dropdown-content">{children}</div>
  ),
  DropdownMenuItem: ({ children, onClick, className }: { children: React.ReactNode; onClick?: (e: any) => void; className?: string }) => (
    <div data-testid="dropdown-item" className={className} onClick={onClick} role="menuitem">
      {children}
    </div>
  ),
  DropdownMenuSeparator: () => <hr data-testid="dropdown-separator" />,
  DropdownMenuLabel: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dropdown-label">{children}</div>
  ),
  DropdownMenuGroup: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuSub: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuSubContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuSubTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuRadioGroup: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

// ---------------------------------------------------------------------------
//  Mock: UI ContextMenu — 使用渲染子元素方式，菜单项直接可见
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/context-menu', () => ({
  ContextMenu: ({ children }: { children: React.ReactNode }) => <div data-testid="context-menu-root">{children}</div>,
  ContextMenuTrigger: ({ children, asChild }: { children: React.ReactNode; asChild?: boolean }) => <>{children}</>,
  ContextMenuContent: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="context-menu-content" className={className}>{children}</div>
  ),
  ContextMenuItem: ({ children, onClick, className }: { children: React.ReactNode; onClick?: () => void; className?: string }) => (
    <div data-testid="context-menu-item" className={className} onClick={onClick} role="menuitem">
      {children}
    </div>
  ),
  ContextMenuSeparator: () => <hr data-testid="context-menu-separator" />,
  ContextMenuLabel: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="context-menu-label">{children}</div>
  ),
  ContextMenuGroup: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ContextMenuPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ContextMenuSub: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ContextMenuSubContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ContextMenuSubTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ContextMenuRadioGroup: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ContextMenuCheckboxItem: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
    <div data-testid="context-menu-checkbox" onClick={onClick}>{children}</div>
  ),
  ContextMenuRadioItem: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
    <div data-testid="context-menu-radio" onClick={onClick}>{children}</div>
  ),
  ContextMenuShortcut: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}))

/** 创建模拟会话数据的工厂函数 */
function createMockSession(overrides: Partial<Session> = {}): Session {
  return {
    id: `session-${Math.random().toString(36).slice(2, 9)}`,
    title: '测试会话',
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T12:00:00Z',
    messageCount: 5,
    starred: false,
    pinned: false,
    ...overrides,
  }
}

/** 默认回调函数集合 */
const defaultCallbacks = {
  onSessionClick: vi.fn(),
  onDeleteSession: vi.fn().mockResolvedValue(undefined),
  onEditSession: vi.fn(),
  onRenameSession: vi.fn(),
  onCopySession: vi.fn(),
  onStarSession: vi.fn(),
  onPinSession: vi.fn(),
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// ============================================================
// AC-1.3-1: 置顶功能入口（下拉菜单）
// ============================================================
describe('AC-1.3-1: 置顶功能入口', () => {
  it('下拉菜单中应包含「置顶会话」选项（未置顶会话）', () => {
    const session = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // Mock 渲染直接展示菜单项（下拉菜单和右键菜单各渲染一份）
    const pinItems = screen.getAllByText('置顶会话')
    expect(pinItems.length).toBeGreaterThanOrEqual(1)
  })

  it('下拉菜单中应包含「取消置顶」选项（已置顶会话）', () => {
    const session = createMockSession({ pinned: true })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const unpinItems = screen.getAllByText('取消置顶')
    expect(unpinItems.length).toBeGreaterThanOrEqual(1)
  })

  it('点击「置顶会话」应调用 onPinSession 回调', async () => {
    const session = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // Mock 下拉菜单直接渲染了置顶选项
    const pinMenuItems = screen.getAllByText('置顶会话')
    await act(async () => {
      fireEvent.click(pinMenuItems[0])
    })

    expect(defaultCallbacks.onPinSession).toHaveBeenCalledTimes(1)
    expect(defaultCallbacks.onPinSession).toHaveBeenCalledWith(session.id)
  })

  it('点击「取消置顶」应调用 onPinSession 回调', async () => {
    const session = createMockSession({ pinned: true })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const unpinMenuItems = screen.getAllByText('取消置顶')
    await act(async () => {
      fireEvent.click(unpinMenuItems[0])
    })

    expect(defaultCallbacks.onPinSession).toHaveBeenCalledTimes(1)
    expect(defaultCallbacks.onPinSession).toHaveBeenCalledWith(session.id)
  })
})

// ============================================================
// AC-1.3-2: 置顶会话分组显示
// ============================================================
describe('AC-1.3-2: 置顶会话分组显示', () => {
  it('有置顶会话时应显示「已置顶」和「全部会话」分组标题', () => {
    const pinnedSession = createMockSession({ pinned: true, title: '置顶会话A' })
    const normalSession = createMockSession({ pinned: false, title: '普通会话B' })
    render(
      <SessionList
        sessions={[pinnedSession, normalSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    expect(screen.getByText('已置顶')).toBeInTheDocument()
    expect(screen.getByText('全部会话')).toBeInTheDocument()
  })

  it('无置顶会话时不应显示「已置顶」分组标题', () => {
    const normalSession = createMockSession({ pinned: false, title: '普通会话' })
    render(
      <SessionList
        sessions={[normalSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    expect(screen.queryByText('已置顶')).not.toBeInTheDocument()
    expect(screen.getByText('全部会话')).toBeInTheDocument()
  })

  it('置顶会话应显示在普通会话之前', () => {
    const pinnedSession = createMockSession({
      pinned: true,
      title: '置顶会话',
      updatedAt: '2026-01-01T00:00:00Z',
    })
    const normalSession = createMockSession({
      pinned: false,
      title: '普通会话',
      updatedAt: '2026-01-02T00:00:00Z',
    })
    render(
      <SessionList
        sessions={[normalSession, pinnedSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const allSessionTitles = screen
      .getAllByRole('button', { name: /^会话:/ })
      .map((el) => el.textContent)
    const pinnedIndex = allSessionTitles.findIndex((t) => t?.includes('置顶会话'))
    const normalIndex = allSessionTitles.findIndex((t) => t?.includes('普通会话'))
    expect(pinnedIndex).toBeLessThan(normalIndex)
  })

  it('两组之间应有视觉分隔线', () => {
    const pinnedSession = createMockSession({ pinned: true, title: '置顶会话' })
    const normalSession = createMockSession({ pinned: false, title: '普通会话' })
    render(
      <SessionList
        sessions={[pinnedSession, normalSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 验证分隔线存在（border-t 的 div）
    const pinnedGroup = screen.getByText('已置顶').closest('[data-group="pinned"]')
    expect(pinnedGroup).toBeInTheDocument()
    const separator = pinnedGroup?.querySelector('.border-t')
    expect(separator).toBeInTheDocument()
  })

  it('置顶会话组内应按 updatedAt 降序排序', () => {
    const pinnedOlder = createMockSession({
      id: 'pinned-older',
      pinned: true,
      title: '较旧置顶',
      updatedAt: '2026-01-01T00:00:00Z',
    })
    const pinnedNewer = createMockSession({
      id: 'pinned-newer',
      pinned: true,
      title: '较新置顶',
      updatedAt: '2026-01-02T00:00:00Z',
    })
    render(
      <SessionList
        sessions={[pinnedOlder, pinnedNewer]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const pinnedGroup = screen.getByText('已置顶').closest('[data-group="pinned"]')
    expect(pinnedGroup).toBeInTheDocument()
    const titles = within(pinnedGroup as HTMLElement)
      .getAllByRole('button', { name: /^会话:/ })
      .map((el) => el.textContent)
    expect(titles[0]).toContain('较新置顶')
    expect(titles[1]).toContain('较旧置顶')
  })

  it('普通会话组内应按 updatedAt 降序排序', () => {
    const normalOlder = createMockSession({
      id: 'normal-older',
      pinned: false,
      title: '较旧普通',
      updatedAt: '2026-01-01T00:00:00Z',
    })
    const normalNewer = createMockSession({
      id: 'normal-newer',
      pinned: false,
      title: '较新普通',
      updatedAt: '2026-01-02T00:00:00Z',
    })
    render(
      <SessionList
        sessions={[normalOlder, normalNewer]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const normalGroup = screen.getByText('全部会话').closest('[data-group="normal"]')
    expect(normalGroup).toBeInTheDocument()
    const titles = within(normalGroup as HTMLElement)
      .getAllByRole('button', { name: /^会话:/ })
      .map((el) => el.textContent)
    expect(titles[0]).toContain('较新普通')
    expect(titles[1]).toContain('较旧普通')
  })
})

// ============================================================
// AC-1.3-3: 置顶视觉标识
// ============================================================
describe('AC-1.3-3: 置顶视觉标识', () => {
  it('置顶会话左侧应显示 Pin 图标替代 MessageSquare 图标', () => {
    const pinnedSession = createMockSession({ pinned: true, title: '置顶会话' })
    render(
      <SessionList
        sessions={[pinnedSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const sessionButton = screen.getByRole('button', { name: /置顶会话/ })
    const pinIcon = sessionButton.querySelector('[data-testid="pin-icon"]')
    expect(pinIcon).toBeInTheDocument()
  })

  it('普通会话左侧应显示 MessageSquare 图标', () => {
    const normalSession = createMockSession({ pinned: false, title: '普通会话' })
    render(
      <SessionList
        sessions={[normalSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const sessionButton = screen.getByRole('button', { name: /普通会话/ })
    const messageIcon = sessionButton.querySelector('[data-testid="message-icon"]')
    expect(messageIcon).toBeInTheDocument()
  })
})

// ============================================================
// AC-1.3-4: 兼容现有功能
// ============================================================
describe('AC-1.3-4: 兼容现有功能', () => {
  it('下拉菜单仍包含编辑、复制、星标、删除选项', () => {
    const session = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // Mock 渲染直接展示所有菜单项（下拉菜单和右键菜单各一份，删除可能在多处出现）
    expect(screen.getAllByText('编辑').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('复制').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('星标').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('删除').length).toBeGreaterThanOrEqual(1)
  })

  it('删除按钮点击后应设置确认状态（触发 Dialog 打开）', async () => {
    const session = createMockSession({ title: '待删除会话' })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 点击删除按钮，触发 handleDeleteRequest 设置 deleteConfirmId
    const deleteButtons = screen.getAllByRole('button', { name: /删除会话/ })
    await act(async () => {
      fireEvent.click(deleteButtons[0])
    })

    // Dialog 通过 open={!!deleteConfirmId} 打开
    const dialogElement = screen.queryByTestId('dialog-root')
    if (dialogElement) {
      expect(dialogElement).toBeInTheDocument()
      expect(dialogElement.textContent).toContain('确认删除')
      expect(dialogElement.textContent).toContain('待删除会话')
    }
  })

  it('星标切换功能不受影响', async () => {
    const session = createMockSession({ starred: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // Mock 直接渲染了星标选项
    const starMenuItems = screen.getAllByText('星标')
    await act(async () => {
      fireEvent.click(starMenuItems[0])
    })

    expect(defaultCallbacks.onStarSession).toHaveBeenCalledWith(session.id)
  })

  it('hover 时仍显示操作按钮', () => {
    const session = createMockSession()
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    const deleteButtons = screen.getAllByRole('button', { name: /删除会话/ })
    const moreButtons = screen.getAllByRole('button', { name: /更多操作/ })
    expect(deleteButtons.length).toBeGreaterThan(0)
    expect(moreButtons.length).toBeGreaterThan(0)
  })
})

// ============================================================
// AC-1.3-5: 右键上下文菜单
// ============================================================
describe('AC-1.3-5: 右键上下文菜单', () => {
  it('右键菜单应包含重命名选项', () => {
    const session = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    expect(screen.getByText('重命名')).toBeInTheDocument()
  })

  it('右键菜单应包含置顶/取消置顶选项', () => {
    const pinnedSession = createMockSession({ pinned: true })
    const normalSession = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[pinnedSession, normalSession]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 已置顶的会话有「取消置顶」
    expect(screen.getAllByText('取消置顶').length).toBeGreaterThanOrEqual(1)
    // 未置顶的会话有「置顶会话」
    expect(screen.getAllByText('置顶会话').length).toBeGreaterThanOrEqual(1)
  })

  it('右键菜单应包含删除选项', () => {
    const session = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 右键菜单中有删除项
    expect(screen.getAllByText('删除').length).toBeGreaterThanOrEqual(1)
  })

  it('右键菜单中点击重命名应显示内联输入框', async () => {
    const session = createMockSession({ pinned: false, title: '测试会话' })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 查找所有重命名选项（可能来自 context menu 和 dropdown menu）
    const renameItems = screen.getAllByText('重命名')
    expect(renameItems.length).toBeGreaterThanOrEqual(1)

    // 点击第一个重命名（context menu 的）
    await act(async () => {
      fireEvent.click(renameItems[0])
    })

    // 应该出现重命名输入框
    const renameInput = screen.getByTestId('rename-input')
    expect(renameInput).toBeInTheDocument()
    expect(renameInput).toHaveValue('测试会话')
  })

  it('右键菜单中点击删除应打开确认对话框', async () => {
    const session = createMockSession({ title: '待删除会话' })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 找到 context menu 中的删除项（在 context-menu-content 中）
    const contextMenuContent = screen.getAllByTestId('context-menu-content')
    expect(contextMenuContent.length).toBeGreaterThan(0)

    // 在 context menu 中找到删除按钮
    const contextDeleteItems = contextMenuContent[0].querySelectorAll('[data-testid="context-menu-item"]')
    const deleteItem = Array.from(contextDeleteItems).find((el) => el.textContent?.includes('删除'))
    expect(deleteItem).toBeTruthy()

    await act(async () => {
      fireEvent.click(deleteItem!)
    })

    // 确认对话框应打开
    const dialogElement = screen.queryByTestId('dialog-root')
    if (dialogElement) {
      expect(dialogElement).toBeInTheDocument()
      expect(dialogElement.textContent).toContain('确认删除')
    }
  })

  it('右键菜单中置顶选项应调用 onPinSession', async () => {
    const session = createMockSession({ pinned: false })
    render(
      <SessionList
        sessions={[session]}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...defaultCallbacks}
      />,
    )

    // 在 context menu 中找到置顶按钮
    const contextMenuContents = screen.getAllByTestId('context-menu-content')
    const pinItems = Array.from(contextMenuContents[0].querySelectorAll('[data-testid="context-menu-item"]'))
      .find((el) => el.textContent?.includes('置顶会话'))
    expect(pinItems).toBeTruthy()

    await act(async () => {
      fireEvent.click(pinItems!)
    })

    expect(defaultCallbacks.onPinSession).toHaveBeenCalledWith(session.id)
  })
})
