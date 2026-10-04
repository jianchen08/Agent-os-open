/** @feature FP-0.2.四 前端 Schema  @vision V6 可即用  @ci frontend-test */
/**
 * useThinkingLevelsQuery 行为测试（思考选项声明数据源 query 化验收）：
 * 1. 有模型名 → GET thinking-levels 携带 model 参数并回填 data；
 * 2. 无模型名/unknown → enabled=false，不发起请求（fetchStatus 停留 idle）；
 * 3. 端点失败 → isError 透出（消费方据此隐藏选择器，不静默造默认选项）。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { ReactNode } from 'react'

const mockGet = vi.fn()

vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
  apiClient: { get: (...args: unknown[]) => mockGet(...args) },
}))

import { useThinkingLevelsQuery } from '../useLlmQueries'

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper, queryClient }
}

beforeEach(() => {
  mockGet.mockReset()
})

describe('useThinkingLevelsQuery — 思考档位声明数据源', () => {
  it('有模型名：携带 model 参数请求并回填选项面', async () => {
    mockGet.mockResolvedValue({
      data: {
        model: 'glm-5.3',
        fields: [{ type: 'select' }],
        options: [{ value: '{"reasoning_effort":"max"}', label: 'max' }],
        current: '{"reasoning_effort":"max"}',
      },
    })
    const { wrapper, queryClient } = makeWrapper()
    const { result } = renderHook(() => useThinkingLevelsQuery('glm-5.3'), { wrapper })
    await waitFor(() => expect(result.current.isLoading).toBe(false), { timeout: 3000 })
    expect(result.current.error ?? null).toBeNull()
    expect(result.current.isSuccess).toBe(true)
    expect(mockGet).toHaveBeenCalledTimes(1)
    const [url, config] = mockGet.mock.calls[0]
    expect(String(url)).toContain('thinking-levels')
    expect((config as { params: { model: string } }).params.model).toBe('glm-5.3')
    // 性质断言：选项 value 是可解析的参数组 JSON（选中即随消息透传的契约）
    for (const opt of result.current.data?.options ?? []) {
      expect(typeof JSON.parse(opt.value)).toBe('object')
    }
    expect(result.current.data?.current).not.toBeNull()
    queryClient.clear()
  })

  it('无模型名 / unknown：enabled=false 不发请求', () => {
    const a = makeWrapper()
    const { result } = renderHook(() => useThinkingLevelsQuery(undefined), { wrapper: a.wrapper })
    expect(result.current.fetchStatus).toBe('idle')
    expect(mockGet).not.toHaveBeenCalled()
    const b = makeWrapper()
    const { result: r2 } = renderHook(() => useThinkingLevelsQuery('unknown'), { wrapper: b.wrapper })
    expect(r2.current.fetchStatus).toBe('idle')
    expect(mockGet).not.toHaveBeenCalled()
    a.queryClient.clear()
    b.queryClient.clear()
  })

  it('端点失败：isError 透出（消费方隐藏选择器，不造默认选项）', async () => {
    mockGet.mockRejectedValue(new Error('network down'))
    const { wrapper, queryClient } = makeWrapper()
    const { result } = renderHook(() => useThinkingLevelsQuery('minimax-m3.1'), { wrapper })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.data).toBeUndefined()
    queryClient.clear()
  })
})
