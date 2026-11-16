/** @feature FP-0.2.移动端适配 移动端会话面收口 | @ci frontend-test */
/**
 * Sidebar 移动端会话点击收工作区覆盖层（真机"视图切换失灵"回归锁）
 *
 * 契约：窄窗/移动端形态下工作区覆盖层（mobileWorkspaceOpen）盖住整个主区，
 * 点击侧栏会话条目 = 用户要回对话主管道视图——抽屉与工作区覆盖层一并收起，
 * 会话切换不得只发生在覆盖层背后（此前缺口：只收抽屉，覆盖层残留，用户视角
 * 主视图"钉死在文件/面板内容视图"）。任务管理/插件面板入口不受影响（它们
 * 本就要点亮覆盖层）。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { getSessions } from '@/services/api/session'
import { useUIStore } from '@/stores/uiStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type { Session } from '@/types/models'

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, getSessions: vi.fn() }
})

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'thread-1',
    title: '可点击的会话',
    createdAt: '2026-10-12T00:00:00Z',
    updatedAt: '2026-10-12T00:00:00Z',
    messageCount: 0,
    status: 'active',
    metadata: {},
    agentId: null,
    workspace: null,
    isolationMode: null,
    pipelineIds: ['pipe-1'],
    pinned: false,
    starred: false,
    ...overrides,
  }
}

describe('Sidebar 移动端会话点击收工作区覆盖层', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useUIStore.setState({ sidebarCollapsed: false, mobileWorkspaceOpen: true })
  })

  it('覆盖层开启时点会话条目：工作区覆盖层与抽屉一并收起（主视图回对话）', async () => {
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })

    const item = await screen.findByRole('button', { name: /会话: 可点击的会话/ })
    expect(useUIStore.getState().mobileWorkspaceOpen).toBe(true)

    fireEvent.click(item)

    // 契约面 = 覆盖层与抽屉收口（会话切换本身由 setActiveSession 既有链路负责）
    await waitFor(() => {
      expect(useUIStore.getState().mobileWorkspaceOpen).toBe(false)
    })
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
  })

  it('覆盖层未开启时点会话条目：状态保持收起（幂等，不误点亮）', async () => {
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    useUIStore.setState({ sidebarCollapsed: true, mobileWorkspaceOpen: false })
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })

    fireEvent.click(await screen.findByRole('button', { name: /会话: 可点击的会话/ }))

    await waitFor(() => {
      expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    })
    expect(useUIStore.getState().mobileWorkspaceOpen).toBe(false)
  })
})
