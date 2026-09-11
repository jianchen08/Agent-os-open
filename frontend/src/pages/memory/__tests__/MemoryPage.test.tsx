/** @ci frontend-test */
/**
 * MemoryPage 页面测试（0% → 主链覆盖批次）。
 *
 * 覆盖：统计卡片、情景记忆列表/空态/分页导航、语义记忆 tab 懒拉取与
 * 失败提示、搜索 tab（空查询不调 API/结果渲染/无结果空态/失败提示）。
 * query 数据经 renderWithProviders 的 QueryClientProvider；memory API 全 mocking。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryPage } from '@/pages/memory/MemoryPage'
import { getSemanticMemory, searchHindsight } from '@/services/api/memory'
import { renderWithProviders } from '@/test/renderWithProviders'

vi.mock('@/services/api/memory', () => ({
  getEpisodes: vi.fn(),
  getMemoryStats: vi.fn(),
  getSemanticMemory: vi.fn(),
  searchHindsight: vi.fn(),
}))

import { getEpisodes, getMemoryStats } from '@/services/api/memory'

const STATS = { episode_count: 7, knowledge_count: 3, total_count: 10 }

const EPISODES = {
  items: [
    {
      id: 'ep-1',
      intent_text: '整理周报',
      execution_summary: '已完成初稿',
      final_score: 0.87,
      created_at: '2026-09-10T08:00:00Z',
      tags: ['工作'],
    },
  ],
  total: 11,
  page: 1,
}

function renderPage() {
  // 默认自动包裹测试 QueryClient（retry:false），无需显式传入
  return renderWithProviders(<MemoryPage />)
}

beforeEach(() => {
  vi.mocked(getMemoryStats).mockResolvedValue(STATS as never)
  vi.mocked(getEpisodes).mockResolvedValue(EPISODES as never)
  vi.mocked(getSemanticMemory).mockResolvedValue({ items: [] } as never)
  vi.mocked(searchHindsight).mockResolvedValue({ items: [], total: 0, query: '' } as never)
})

describe('MemoryPage: 统计与情景记忆', () => {
  it('渲染统计卡片三项计数', async () => {
    renderPage()
    await screen.findByText('7') // 统计卡数字（tab 文案与卡标签歧义，以数据到达为准）
    expect(screen.getByText('语义知识')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('总记忆数')).toBeTruthy()
    expect(screen.getByText('10')).toBeTruthy()
  })

  it('渲染情景记忆列表条目与标签', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('整理周报')).toBeTruthy())
    expect(screen.getByText('已完成初稿')).toBeTruthy()
    expect(screen.getByText('0.87')).toBeTruthy()
    expect(screen.getByText('工作')).toBeTruthy()
  })

  it('总数超页容量时显示分页导航，下一页拉新页', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('1 / 2')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await waitFor(() => expect(vi.mocked(getEpisodes)).toHaveBeenCalledWith(2, 10))
  })

  it('情景记忆为空 → 空态引导文案', async () => {
    vi.mocked(getEpisodes).mockResolvedValue({ items: [], total: 0, page: 1 } as never)
    renderPage()
    await waitFor(() => expect(screen.getByText('暂无情景记忆')).toBeTruthy())
  })

  it('情景记忆拉取失败 → 错误提示', async () => {
    vi.mocked(getEpisodes).mockRejectedValue(new Error('网络故障'))
    renderPage()
    await waitFor(() => expect(screen.getByText('网络故障')).toBeTruthy())
  })
})

describe('MemoryPage: 语义记忆 tab', () => {
  it('切换时懒拉取并渲染语义条目', async () => {
    vi.mocked(getSemanticMemory).mockResolvedValue({
      items: [{ id: 'sm-1', content: '用户偏好深色主题', source_type: 'auto', created_at: '2026-09-10T08:00:00Z' }],
    } as never)
    renderPage()
    await screen.findByText('整理周报') // 首页数据到位后再切 tab
    fireEvent.click(screen.getByRole('button', { name: '语义记忆' }))
    await waitFor(() => expect(screen.getByText('用户偏好深色主题')).toBeTruthy())
    expect(vi.mocked(getSemanticMemory)).toHaveBeenCalledTimes(1)
  })

  it('语义记忆为空 → 空态文案', async () => {
    renderPage()
    await screen.findByText('整理周报')
    fireEvent.click(screen.getByRole('button', { name: '语义记忆' }))
    await waitFor(() => expect(screen.getByText('暂无语义记忆')).toBeTruthy())
  })

  it('语义拉取失败 → 错误提示', async () => {
    vi.mocked(getSemanticMemory).mockRejectedValue(new Error('语义服务不可用'))
    renderPage()
    await screen.findByText('整理周报')
    fireEvent.click(screen.getByRole('button', { name: '语义记忆' }))
    await waitFor(() => expect(screen.getByText('语义服务不可用')).toBeTruthy())
  })
})

describe('MemoryPage: 搜索 tab', () => {
  function searchSubmit(): HTMLElement {
    // tab 与提交按钮可访问名同为「搜索」：切换后取第二个（表单提交钮）
    const buttons = screen.getAllByRole('button', { name: '搜索' })
    return buttons[buttons.length - 1]
  }

  async function gotoSearch() {
    renderPage()
    await screen.findByText('整理周报')
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))
    await screen.findByPlaceholderText('搜索记忆...')
  }

  it('空查询不触发搜索 API', async () => {
    await gotoSearch()
    fireEvent.click(searchSubmit())
    expect(vi.mocked(searchHindsight)).not.toHaveBeenCalled()
  })

  it('有结果：渲染命中数与条目相关度', async () => {
    vi.mocked(searchHindsight).mockResolvedValue({
      items: [
        { id: 'r1', content: '部署脚本要点', memory_type: 'episodic', score: 0.92, created_at: '2026-09-10T08:00:00Z' },
      ],
      total: 1,
      query: '部署',
    } as never)
    await gotoSearch()
    fireEvent.change(screen.getByPlaceholderText('搜索记忆...'), { target: { value: '部署' } })
    fireEvent.click(searchSubmit())
    await waitFor(() => expect(screen.getByText('找到 1 条结果')).toBeTruthy())
    expect(screen.getByText('部署脚本要点')).toBeTruthy()
    expect(screen.getByText('相关度: 0.92')).toBeTruthy()
  })

  it('无结果：空态引导', async () => {
    vi.mocked(searchHindsight).mockResolvedValue({ items: [], total: 0, query: 'xyz' } as never)
    await gotoSearch()
    fireEvent.change(screen.getByPlaceholderText('搜索记忆...'), { target: { value: 'xyz' } })
    fireEvent.click(searchSubmit())
    await waitFor(() => expect(screen.getByText('无搜索结果')).toBeTruthy())
  })

  it('搜索失败：错误文案', async () => {
    vi.mocked(searchHindsight).mockRejectedValue(new Error('搜索服务超时'))
    await gotoSearch()
    fireEvent.change(screen.getByPlaceholderText('搜索记忆...'), { target: { value: '部署' } })
    fireEvent.click(searchSubmit())
    await waitFor(() => expect(screen.getByText('搜索服务超时')).toBeTruthy())
  })
})
