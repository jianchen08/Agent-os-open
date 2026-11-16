// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * 复制会话 pending 与失败反馈测试（#8）：
 * - SessionList：copyingSessionIds 命中行 → 行内 Loader2（aria 复制中）+ 行禁用
 *   （复制 await 窗口可见进行中态，防重复点击）
 * - Sidebar：copySession 失败 → 通知落在操作点（复制会话失败），pending 复位
 */
import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionList } from '@/components/session/SessionList'
import type * as SessionApiModule from '@/services/api/session'
import type { Session } from '@/types/models'

const sessionApiMocks = vi.hoisted(() => ({ getSessions: vi.fn() }))
vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<typeof SessionApiModule>()
  return {
    ...actual,
    getSessions: (...args: unknown[]) => sessionApiMocks.getSessions(...args),
  }
})

const { Sidebar } = await import('@/components/layout/Sidebar')
const { useAuthStore } = await import('@/stores/authStore')
const { useNotificationStore } = await import('@/stores/notificationStore')
const { useSessionListStore } = await import('@/stores/sessionListStore')
const { useSessionStore } = await import('@/stores/sessionStore')
const { createTestQueryClient, renderWithProviders } = await import('@/test/renderWithProviders')

const SESSION: Session = {
  id: 's-copy',
  title: '待复制会话',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
  messageCount: 0,
  pipelineIds: [],
} as Session

/** copySession 可控桩（Sidebar 用例覆写 store 动作时引用） */
const copyMocks = vi.hoisted(() => ({ copySession: vi.fn() }))

beforeEach(() => {
  vi.clearAllMocks()
  useNotificationStore.setState({ notifications: [] })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SessionList 复制行级 pending', () => {
  const baseProps = {
    sessions: [SESSION],
    activeSessionId: null,
    deletingSessionIds: new Set<string>(),
    onSessionClick: vi.fn(),
    onDeleteSession: vi.fn(async () => {}),
    onEditSession: vi.fn(),
    onCopySession: vi.fn(),
    onStarSession: vi.fn(),
    onPinSession: vi.fn(),
  }

  it('copyingSessionIds 命中行 → 行内转圈（复制中）+ 行禁用', () => {
    render(<SessionList {...baseProps} copyingSessionIds={new Set(['s-copy'])} />)
    expect(screen.getByLabelText('复制中')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /待复制会话/ })).toHaveClass('pointer-events-none')
  })

  it('未命中行 → 无转圈、行可点（空闲态不误伤）', () => {
    render(<SessionList {...baseProps} copyingSessionIds={new Set(['s-other'])} />)
    expect(screen.queryByLabelText('复制中')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /待复制会话/ })).not.toHaveClass('pointer-events-none')
  })
})

describe('Sidebar 复制会话失败反馈（操作点通知 + pending 复位）', () => {
  beforeEach(() => {
    useAuthStore.setState({
      isAuthenticated: true,
      user: {
        id: 'user-1', username: 'alice', email: 'a@x.com', role: 'admin',
        createdAt: '2026-01-01T00:00:00Z',
      },
      token: 'test-token',
      mustChangePassword: false,
    })
    // copySession 以可控桩覆写（真实实现会走建会话 API 链，非本用例关注面）
    useSessionListStore.setState({ copySession: copyMocks.copySession })
    useSessionStore.setState({ activeSessionId: null })
  })

  it('copySession 失败 → 「复制会话失败」通知 + 行 pending 落定复位', async () => {
    let rejectCopy: (e: unknown) => void = () => {}
    copyMocks.copySession.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectCopy = reject
      }),
    )
    sessionApiMocks.getSessions.mockResolvedValue([SESSION])

    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })

    // 打开会话行三点菜单 → 点复制
    fireEvent.click(await screen.findByText('待复制会话'))
    const moreBtn = await screen.findByRole('button', { name: '更多操作' })
    fireEvent.pointerDown(moreBtn)
    fireEvent.pointerUp(moreBtn)
    fireEvent.click(moreBtn)
    fireEvent.click(await screen.findByText('复制'))

    // await 窗口：行内转圈
    expect(await screen.findByLabelText('复制中')).toBeInTheDocument()

    rejectCopy({ message: '磁盘满' })
    await waitFor(() =>
      expect(
        useNotificationStore
          .getState()
          .notifications.some((n) => n.title === '复制会话失败' && n.message === '磁盘满'),
      ).toBe(true),
    )
    await waitFor(() => expect(screen.queryByLabelText('复制中')).not.toBeInTheDocument())
  })
})
