// @feature: FP-0.2.六 记忆检索(BUG-76 记忆页操作面) | @ci: frontend-test
/** @ci frontend-test */
/**
 * 记忆页列表项操作面测试：查看详情（by-id 端点）+ 删除单条（不可逆，两步确认）。
 *
 * - 情景/语义条目均有查看与删除入口，走 getMemoryById/deleteMemoryById；
 * - 删除需两步确认：点击删除不直接调 API，确认才调；成功后统计与情景分页失效
 *   重拉（被删条目从列表消失）；
 * - 详情/删除失败给页面错误提示，不静默。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryPage } from '@/pages/memory/MemoryPage'
import {
  deleteMemoryById,
  getEpisodes,
  getMemoryById,
  getMemoryStats,
  getSemanticMemory,
  searchHindsight,
} from '@/services/api/memory'
import { renderWithProviders } from '@/test/renderWithProviders'

vi.mock('@/services/api/memory', async () => (await import('./memoryPageTestUtils')).memoryApiModuleMock())

const STATS = { episode_count: 1, knowledge_count: 0, total_count: 1 }

const EPISODES = {
  items: [
    {
      id: 'ep-1',
      intent_text: '整理周报',
      created_at: '2026-09-10T08:00:00Z',
      tags: ['工作'],
    },
  ],
  total: 1,
  page: 1,
}

const DETAIL = {
  id: 'ep-1',
  content: '整理周报全文内容（截断前的原文）',
  memory_type: 'episode',
  tags: ['工作'],
  score: 0,
  created_at: '2026-09-10T08:00:00Z',
}

function renderPage() {
  return renderWithProviders(<MemoryPage />)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getMemoryStats).mockResolvedValue(STATS as never)
  vi.mocked(getEpisodes).mockResolvedValue(EPISODES as never)
  vi.mocked(getSemanticMemory).mockResolvedValue({ items: [] } as never)
  vi.mocked(searchHindsight).mockResolvedValue({ items: [], total: 0, query: '' } as never)
  vi.mocked(getMemoryById).mockResolvedValue(DETAIL as never)
  vi.mocked(deleteMemoryById).mockResolvedValue({ message: '记忆已删除' } as never)
})

describe('MemoryPage 条目操作：查看详情', () => {
  it('情景条目查看详情：调 by-id 端点并在对话框展示全文', async () => {
    renderPage()
    await screen.findByText('整理周报')

    fireEvent.click(screen.getByRole('button', { name: '查看记忆详情 ep-1' }))

    await waitFor(() => expect(vi.mocked(getMemoryById)).toHaveBeenCalledWith('ep-1'))
    await waitFor(() => expect(screen.getByText('整理周报全文内容（截断前的原文）')).toBeTruthy())
    expect(screen.getByText('记忆详情')).toBeTruthy()
  })

  it('详情请求失败：对话框不展示空内容并给页面错误提示', async () => {
    vi.mocked(getMemoryById).mockRejectedValue({ message: '未找到相关记忆' } as never)
    renderPage()
    await screen.findByText('整理周报')

    fireEvent.click(screen.getByRole('button', { name: '查看记忆详情 ep-1' }))

    await waitFor(() => expect(screen.getByText('未找到相关记忆')).toBeTruthy())
  })
})

describe('MemoryPage 条目操作：删除单条（不可逆两步确认）', () => {
  it('点击删除先出确认层，此时不调删除 API；取消后仍不调', async () => {
    renderPage()
    await screen.findByText('整理周报')

    fireEvent.click(screen.getByRole('button', { name: '删除记忆 ep-1' }))

    expect(screen.getByRole('button', { name: '确认删除' })).toBeTruthy()
    expect(vi.mocked(deleteMemoryById)).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('button', { name: '确认删除' })).toBeNull()
    expect(vi.mocked(deleteMemoryById)).not.toHaveBeenCalled()
  })

  it('确认删除：调 by-id 删除端点并失效重拉（条目消失）', async () => {
    vi.mocked(getEpisodes).mockResolvedValueOnce(EPISODES as never).mockResolvedValueOnce({ items: [], total: 0, page: 1 } as never)
    renderPage()
    await screen.findByText('整理周报')

    fireEvent.click(screen.getByRole('button', { name: '删除记忆 ep-1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => expect(vi.mocked(deleteMemoryById)).toHaveBeenCalledWith('ep-1'))
    // 统计与情景分页失效重拉：getEpisodes 第二次调用返回空 → 条目消失
    await waitFor(() => expect(vi.mocked(getEpisodes)).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText('整理周报')).toBeNull())
    // 统计在删除后至少重拉一次（初始挂载之外；React Query 客户端可能并发去重/补拉，不断言精确次数）
    expect(vi.mocked(getMemoryStats).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('删除失败：错误提示且条目保留', async () => {
    vi.mocked(deleteMemoryById).mockRejectedValue({ message: '删除失败：后端不可用' } as never)
    renderPage()
    await screen.findByText('整理周报')

    fireEvent.click(screen.getByRole('button', { name: '删除记忆 ep-1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => expect(screen.getByText('删除失败：后端不可用')).toBeTruthy())
    expect(screen.getByText('整理周报')).toBeTruthy()
  })
})

describe('MemoryPage 条目操作：语义记忆', () => {
  async function gotoSemantic() {
    renderPage()
    await screen.findByText('整理周报')
    fireEvent.click(screen.getByRole('button', { name: '语义记忆' }))
    await screen.findByText('用户偏好深色主题')
  }

  beforeEach(() => {
    vi.mocked(getSemanticMemory).mockResolvedValue({
      items: [{ id: 'sm-1', content: '用户偏好深色主题', source_type: 'memory_backend', created_at: '2026-09-10T08:00:00Z' }],
    } as never)
  })

  it('语义条目具备查看/删除入口，查看走 by-id 端点', async () => {
    await gotoSemantic()

    expect(screen.getByRole('button', { name: '查看记忆详情 sm-1' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '删除记忆 sm-1' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '查看记忆详情 sm-1' }))
    await waitFor(() => expect(vi.mocked(getMemoryById)).toHaveBeenCalledWith('sm-1'))
  })

  it('语义条目删除成功后从本地列表剔除', async () => {
    await gotoSemantic()

    fireEvent.click(screen.getByRole('button', { name: '删除记忆 sm-1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => expect(vi.mocked(deleteMemoryById)).toHaveBeenCalledWith('sm-1'))
    await waitFor(() => expect(screen.queryByText('用户偏好深色主题')).toBeNull())
  })

  it('语义条目删除可取消：取消后不调 API 且条目保留', async () => {
    await gotoSemantic()

    fireEvent.click(screen.getByRole('button', { name: '删除记忆 sm-1' }))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    expect(vi.mocked(deleteMemoryById)).not.toHaveBeenCalled()
    expect(screen.getByText('用户偏好深色主题')).toBeTruthy()
  })
})
