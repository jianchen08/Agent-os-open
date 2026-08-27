/**
 * Sidebar 消息搜索命中点击跳转测试
 *
 * 覆盖消息搜索结果点击的核心契约：
 * - 命中 session_id 是管道 ID（12hex），须解析归属会话（activePipelineId 优先，
 *   其次 pipelineIds 包含，旧数据 thread_id==pipeline_id 兜底）后再切会话；
 * - 定位目标（pipelineId + sequence）写入 uiStore，由 ChatContainer 消费；
 * - 无归属会话（孤儿管道）不跳转、不写定位目标。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { searchGlobal } from '@/services/api/search'
import { getSessions } from '@/services/api/session'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type { Session } from '@/types/models'

vi.mock('@/services/api/search', () => ({
  searchGlobal: vi.fn(),
}))

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, getSessions: vi.fn() }
})

/** 构造测试会话（主管道 pipe-abc，id=thread-1，与管道 ID 不同值——0.2 现实） */
function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'thread-1',
    title: '测试会话',
    createdAt: '2026-08-27T00:00:00Z',
    updatedAt: '2026-08-27T00:00:00Z',
    messageCount: 3,
    status: 'active',
    metadata: {},
    agentId: 'agentos',
    workspace: null,
    isolationMode: null,
    pipelineIds: ['pipe-abc'],
    activePipelineId: 'pipe-abc',
    pinned: false,
    starred: false,
    ...overrides,
  }
}

const searchHit = {
  id: 'msg-1',
  session_id: 'pipe-abc',
  role: 'user',
  content: '包含关键词的消息内容',
  timestamp: '2026-08-27T00:00:00Z',
  sequence: 5,
}

describe('Sidebar 消息搜索命中跳转', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 重置跨测试共享的 store 状态
    useUIStore.setState({ messageJump: null, messageSearchQuery: '' })
    useSessionStore.setState({ activeSessionId: null })
    vi.mocked(searchGlobal).mockResolvedValue({
      query: '关键词',
      type: 'all',
      sessions: [],
      messages: [searchHit],
    })
  })

  /** 渲染 Sidebar 并触发一次搜索（输入 → 防抖 350ms → 结果渲染） */
  async function renderAndSearch() {
    const queryClient = createTestQueryClient()
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    renderWithProviders(<Sidebar />, { queryClient })
    const input = screen.getByPlaceholderText('搜索会话和消息...')
    fireEvent.change(input, { target: { value: '关键词' } })
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-message-results')).toBeInTheDocument()
    })
    return queryClient
  }

  it('点击消息命中：解析管道归属会话 → 切会话 + 写入定位目标', async () => {
    const setActiveSessionSpy = vi
      .spyOn(useSessionListStore.getState(), 'setActiveSession')
      .mockResolvedValue(undefined)
    try {
      await renderAndSearch()

      fireEvent.click(screen.getByText('包含关键词的消息内容'))

      // 切到归属会话（会话 id=thread-1，非管道 ID）
      expect(setActiveSessionSpy).toHaveBeenCalledWith('thread-1')
      // 定位目标已写入 uiStore（管道 ID + sequence），供 ChatContainer 消费
      expect(useUIStore.getState().messageJump).toEqual({
        pipelineId: 'pipe-abc',
        sequence: 5,
      })
    } finally {
      setActiveSessionSpy.mockRestore()
    }
  })

  it('命中子管道（pipelineIds 包含，非 activePipelineId）仍解析到归属会话', async () => {
    const setActiveSessionSpy = vi
      .spyOn(useSessionListStore.getState(), 'setActiveSession')
      .mockResolvedValue(undefined)
    const queryClient = createTestQueryClient()
    // activePipelineId 是主管道，消息命中子管道 pipe-sub
    vi.mocked(getSessions).mockResolvedValue([
      makeSession({ activePipelineId: 'pipe-abc', pipelineIds: ['pipe-abc', 'pipe-sub'] }),
    ])
    vi.mocked(searchGlobal).mockResolvedValue({
      query: '关键词',
      type: 'all',
      sessions: [],
      messages: [{ ...searchHit, session_id: 'pipe-sub' }],
    })
    try {
      renderWithProviders(<Sidebar />, { queryClient })
      const input = screen.getByPlaceholderText('搜索会话和消息...')
      fireEvent.change(input, { target: { value: '关键词' } })
      await waitFor(() => {
        expect(screen.getByTestId('sidebar-message-results')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByText('包含关键词的消息内容'))

      expect(setActiveSessionSpy).toHaveBeenCalledWith('thread-1')
      expect(useUIStore.getState().messageJump).toEqual({
        pipelineId: 'pipe-sub',
        sequence: 5,
      })
    } finally {
      setActiveSessionSpy.mockRestore()
    }
  })

  it('旧数据 thread_id==pipeline_id：按会话 id 兜底仍可跳转', async () => {
    const setActiveSessionSpy = vi
      .spyOn(useSessionListStore.getState(), 'setActiveSession')
      .mockResolvedValue(undefined)
    const queryClient = createTestQueryClient()
    vi.mocked(getSessions).mockResolvedValue([
      makeSession({ id: 'pipe-legacy', activePipelineId: null, pipelineIds: [] }),
    ])
    vi.mocked(searchGlobal).mockResolvedValue({
      query: '关键词',
      type: 'all',
      sessions: [],
      messages: [{ ...searchHit, session_id: 'pipe-legacy' }],
    })
    try {
      renderWithProviders(<Sidebar />, { queryClient })
      const input = screen.getByPlaceholderText('搜索会话和消息...')
      fireEvent.change(input, { target: { value: '关键词' } })
      await waitFor(() => {
        expect(screen.getByTestId('sidebar-message-results')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByText('包含关键词的消息内容'))

      expect(setActiveSessionSpy).toHaveBeenCalledWith('pipe-legacy')
      expect(useUIStore.getState().messageJump).toEqual({
        pipelineId: 'pipe-legacy',
        sequence: 5,
      })
    } finally {
      setActiveSessionSpy.mockRestore()
    }
  })

  it('孤儿管道（无归属会话）不跳转、不写定位目标', async () => {
    const setActiveSessionSpy = vi
      .spyOn(useSessionListStore.getState(), 'setActiveSession')
      .mockResolvedValue(undefined)
    const queryClient = createTestQueryClient()
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    vi.mocked(searchGlobal).mockResolvedValue({
      query: '关键词',
      type: 'all',
      sessions: [],
      messages: [{ ...searchHit, session_id: 'pipe-orphan' }],
    })
    try {
      renderWithProviders(<Sidebar />, { queryClient })
      const input = screen.getByPlaceholderText('搜索会话和消息...')
      fireEvent.change(input, { target: { value: '关键词' } })
      await waitFor(() => {
        expect(screen.getByTestId('sidebar-message-results')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByText('包含关键词的消息内容'))

      expect(setActiveSessionSpy).not.toHaveBeenCalled()
      expect(useUIStore.getState().messageJump).toBeNull()
    } finally {
      setActiveSessionSpy.mockRestore()
    }
  })

  it('sequence 缺失（旧数据/异常行）：仍切会话但不写定位目标', async () => {
    const setActiveSessionSpy = vi
      .spyOn(useSessionListStore.getState(), 'setActiveSession')
      .mockResolvedValue(undefined)
    const queryClient = createTestQueryClient()
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    vi.mocked(searchGlobal).mockResolvedValue({
      query: '关键词',
      type: 'all',
      sessions: [],
      messages: [{ ...searchHit, sequence: undefined as unknown as number }],
    })
    try {
      renderWithProviders(<Sidebar />, { queryClient })
      const input = screen.getByPlaceholderText('搜索会话和消息...')
      fireEvent.change(input, { target: { value: '关键词' } })
      await waitFor(() => {
        expect(screen.getByTestId('sidebar-message-results')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByText('包含关键词的消息内容'))

      expect(setActiveSessionSpy).toHaveBeenCalledWith('thread-1')
      expect(useUIStore.getState().messageJump).toBeNull()
    } finally {
      setActiveSessionSpy.mockRestore()
    }
  })
})
