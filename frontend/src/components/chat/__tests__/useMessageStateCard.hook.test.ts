/** @feature FP-0.2.四 前端Schema(状态统一标记机制) | @ci: frontend-test */
/**
 * useMessageStateCard hook 车道——三道门分发（active/载荷/声明）。
 *
 * mock 仅外部依赖（端点 apiClient、声明 registry、样式解析），会话列表走
 * readSessions 真实 query 缓存（写入后读取），hook 渲染经 renderHook 真链。
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readSessions, updateSessionsCache } from '@/hooks/queries/useSessionsQuery'
import { queryClient } from '@/services/query/queryClient'
import { useSessionStore } from '@/stores/sessionStore'

// 被测模块按命名导入 { apiClient } 消费，其他链路多走 default——双形态同实例
vi.mock('@/services/api/client', () => {
  const apiClient = { get: vi.fn() }
  return { default: apiClient, apiClient }
})

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    getAllMessageCards: vi.fn(() => [
      { match: { marker: 'state' }, style_id: 'roleplay_state_card' },
    ]),
  },
}))

vi.mock('@/components/chat/PluginMessageCard', () => ({
  resolveMessageStyle: vi.fn((styleId: string) => (styleId === 'roleplay_state_card' ? {} : null)),
}))

import { apiClient } from '@/services/api/client'
import { useMessageStateCard } from '@/hooks/queries/useMessageStateCard'

describe('useMessageStateCard — 三道门分发', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    queryClient.clear()
    useSessionStore.setState({ activeSessionId: 'sess-a' })
    updateSessionsCache(() => [{ id: 'sess-a', pipelineIds: ['pipe-main'] } as never])
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { state_updates: { entries: { mood: '平静' }, span: [0, 10], ts: 't1' } },
    })
  })

  afterEach(() => {
    useSessionStore.setState({ activeSessionId: null })
    queryClient.clear()
  })

  it('active + 载荷 + 声明命中 → 分发卡片（styleId/entries 透传）', async () => {
    const { result } = renderHook(() => useMessageStateCard(true))
    await waitFor(() => expect(result.current).not.toBeNull())
    // 逐步断言：任一字段漂移时失败信息可定位（entries 经 payload 原样透传）
    expect(result.current).toMatchObject({
      styleId: 'roleplay_state_card',
      entries: { mood: '平静' },
      span: [0, 10],
    })
  })

  it('active=false → 零请求零渲染（门 1）', async () => {
    const { result } = renderHook(() => useMessageStateCard(false))
    expect(result.current).toBeNull()
    expect(apiClient.get).not.toHaveBeenCalled()
  })

  it('载荷缺失（state_updates null）→ null（门 2）', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { state_updates: null } })
    const { result } = renderHook(() => useMessageStateCard(true))
    await waitFor(() => expect(apiClient.get).toHaveBeenCalled())
    expect(result.current).toBeNull()
  })

  it('会话列表无该会话（管道不可得）→ null 且零请求', async () => {
    updateSessionsCache(() => [])
    const { result } = renderHook(() => useMessageStateCard(true))
    // 等一拍让 effect 走完早退分支
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current).toBeNull()
    expect(apiClient.get).not.toHaveBeenCalled()
    expect(readSessions()).toEqual([])
  })

  it('声明缺失（registry 无 state 卡声明）→ null（门 3，宿主零渲染语义）', async () => {
    const { contributionRegistry } = await import('@/services/schema/ContributionRegistry')
    vi.mocked(contributionRegistry.getAllMessageCards).mockReturnValue([])
    const { result } = renderHook(() => useMessageStateCard(true))
    await waitFor(() => expect(apiClient.get).toHaveBeenCalled())
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('端点拒绝 → 静默 null（有界失败）', async () => {
    vi.mocked(apiClient.get).mockRejectedValue(new Error('network'))
    const { result } = renderHook(() => useMessageStateCard(true))
    await waitFor(() => expect(apiClient.get).toHaveBeenCalled())
    await waitFor(() => expect(result.current).toBeNull())
  })
})
