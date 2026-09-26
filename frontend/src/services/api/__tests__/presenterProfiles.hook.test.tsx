/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * presenterProfiles hook 车道补测（presenterProfiles.test.ts 已覆盖纯函数车道，
 * 这里补 usePresenterProfile 数据链：queryFn → fetchPresenterProfile 的
 * 端点取数 + 卡目录解析，以及 gate 双出口的 null 语义）。
 *
 * 外部依赖 mock：apiClient（网络传输）；react-query 走真实实现
 * （QueryClient retry 关闭，呈现档案失败不重试的契约在 hook 内声明）。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...args: unknown[]) => apiGet(...args) },
}))

import { usePresenterProfile } from '../presenterProfiles'

const CARDS = [
  { id: 'card_luna', name: '月见', avatar: '🌙' },
  { id: 'card_bare', name: '素卡' },
]

function makeWrapper(): (props: { children: ReactNode }) => ReturnType<typeof createElement> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return ({ children }) => createElement(QueryClientProvider, { client }, children)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('usePresenterProfile · 模式包卡呈现档案', () => {
  it('mode_roleplay 卡命中：经插件数据端点取卡目录并解析出 {name, avatar, origin}', async () => {
    apiGet.mockResolvedValue({ data: { cards: CARDS } })
    const { result } = renderHook(() => usePresenterProfile('mode_roleplay/card_luna'), {
      wrapper: makeWrapper(),
    })
    await waitFor(() => expect(result.current).not.toBeNull())
    expect(result.current).toEqual({ name: '月见', avatar: '🌙', origin: 'mode_roleplay' })
    expect(apiGet).toHaveBeenCalledWith('/ext/mode_roleplay/data/cards')
  })

  it('avatar 缺省卡归 null（不伪造空值）——同链路第二组输入', async () => {
    apiGet.mockResolvedValue({ data: { cards: CARDS } })
    const { result } = renderHook(() => usePresenterProfile('mode_roleplay/card_bare'), {
      wrapper: makeWrapper(),
    })
    await waitFor(() => expect(result.current).not.toBeNull())
    expect(result.current).toEqual({ name: '素卡', avatar: null, origin: 'mode_roleplay' })
  })

  it('id 不在卡目录：null（端点可达但查无此卡）', async () => {
    apiGet.mockResolvedValue({ data: { cards: [] } })
    const { result } = renderHook(() => usePresenterProfile('mode_roleplay/card_ghost'), {
      wrapper: makeWrapper(),
    })
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('裸 agent_id（无模式前缀）：零请求直接 null（走 agents 注册表老路）', async () => {
    const { result } = renderHook(() => usePresenterProfile('main'), { wrapper: makeWrapper() })
    expect(result.current).toBeNull()
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('agentId 缺席：null 且零请求', () => {
    const { result } = renderHook(() => usePresenterProfile(undefined), { wrapper: makeWrapper() })
    expect(result.current).toBeNull()
    expect(apiGet).not.toHaveBeenCalled()
  })
})
