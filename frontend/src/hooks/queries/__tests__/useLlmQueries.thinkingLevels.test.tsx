/** @feature FP-0.2.四 思考参数组选项 query（useThinkingLevelsQuery） | @ci: frontend-test */
/**
 * 模型思考参数组选项 query 直测：模型名有效时经 CONFIG.LLM_THINKING_LEVELS
 * 拉取平铺选项；'unknown'/未传模型不发起请求（enabled 门）。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getMock = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({ default: { get: (...args: unknown[]) => getMock(...args) } }))

import { API_ENDPOINTS } from '@/constants/api'
import { useThinkingLevelsQuery } from '../useLlmQueries'

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

beforeEach(() => {
  getMock.mockReset()
})

describe('useThinkingLevelsQuery 思考参数组选项', () => {
  it('有效模型名：按 model 参数拉取并返回选项/当前值', async () => {
    getMock.mockResolvedValue({
      data: {
        fields: [{ thinking_strength: 'high' }],
        options: [{ value: '{"level":"high"}', label: '深度' }],
        current: '{"level":"high"}',
      },
    })
    const { result } = renderHook(() => useThinkingLevelsQuery('gpt-5'), {
      wrapper: wrapper(makeClient()),
    })
    await waitFor(() => expect(result.current.data?.options).toHaveLength(1))
    expect(getMock).toHaveBeenCalledWith(
      API_ENDPOINTS.CONFIG.LLM_THINKING_LEVELS,
      expect.objectContaining({ params: { model: 'gpt-5' } }),
    )
    expect(result.current.data?.current).toBe('{"level":"high"}')
  })

  it('模型名 unknown/未传：enabled 门关，不发请求', () => {
    const { result } = renderHook(() => useThinkingLevelsQuery('unknown'), {
      wrapper: wrapper(makeClient()),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(getMock).not.toHaveBeenCalled()
  })
})
