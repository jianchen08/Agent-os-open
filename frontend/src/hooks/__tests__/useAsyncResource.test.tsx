// @feature: FP-T12 前端适配 | @ci: frontend-test
/**
 * useAsyncResource 四态契约测试（OBS-R258-1）
 *
 * 核验页面级异步资源四态派生的可观察行为：
 * - loading → ready / empty / error 穷举转移，status 是渲染分支唯一依据
 * - 失败优先于旧数据（refetch 失败不得被上一次成功数据掩盖成 ready）
 * - 非 Error 拒绝回退 fallbackErrorText（不产出 [object Object] 类脏文本）
 * - 空态判定：数组默认长度 0，非数组数据须显式 isEmpty
 *
 * 测试策略：仅 mock 外部边界（queryFn 背后的网络层取数函数），
 * useQuery/useAsyncResource 走真实链（renderWithProviders 的测试 QueryClient：
 * retry:false 保证失败态确定性）。
 */

import { QueryClientProvider, useQuery } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createTestQueryClient } from '@/test/renderWithProviders'
import { useAsyncResource } from '../useAsyncResource'
import type { QueryClientProviderProps } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const fetchList = vi.fn<() => Promise<string[]>>()

function makeWrapper() {
  const client = createTestQueryClient()
  return function Wrapper({ children }: { children: ReactNode }) {
    const props = { client, children } as unknown as QueryClientProviderProps
    return <QueryClientProvider {...props} />
  }
}

function renderResource(
  options?: Parameters<typeof useAsyncResource>[1],
) {
  return renderHook(
    () => {
      const query = useQuery({
        queryKey: ['probe', 'use-async-resource'],
        queryFn: fetchList,
      })
      return useAsyncResource(query, options)
    },
    { wrapper: makeWrapper() },
  )
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('useAsyncResource — 四态派生', () => {
  it('首载为 loading，数据到达后转 ready 且 data 可读', async () => {
    fetchList.mockResolvedValue(['a', 'b'])
    const { result } = renderResource()

    expect(result.current.status).toBe('loading')
    expect(result.current.isLoading).toBe(true)

    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.data).toEqual(['a', 'b'])
    expect(result.current.isLoading).toBe(false)
    expect(result.current.isError).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('拒绝 Error → status error 且文案取 Error.message', async () => {
    fetchList.mockRejectedValue(new Error('后端不可达'))
    const { result } = renderResource()

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(result.current.isError).toBe(true)
    expect(result.current.error).toBe('后端不可达')
    expect(result.current.data).toBeUndefined()
  })

  it('非 Error 拒绝 → 回退 fallbackErrorText（不展示 [object Object] 脏文本）', async () => {
    fetchList.mockRejectedValue({ code: 'E_IO' })
    const { result } = renderResource({ fallbackErrorText: '获取列表失败' })

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(result.current.error).toBe('获取列表失败')
  })

  it('非 Error 拒绝且未给 fallbackErrorText → 回退默认文案', async () => {
    fetchList.mockRejectedValue('boom')
    const { result } = renderResource()

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(result.current.error).toBe('加载失败，请稍后重试')
  })

  it('空数组（默认空判定）→ status empty', async () => {
    fetchList.mockResolvedValue([])
    const { result } = renderResource()

    await waitFor(() => expect(result.current.status).toBe('empty'))
    expect(result.current.isError).toBe(false)
  })

  it('非数组数据：默认判定非空 → ready；显式 isEmpty 命中 → empty', async () => {
    fetchList.mockResolvedValue({ items: [], total: 0 })
    const { result: defaultPredicate } = renderResource()
    await waitFor(() => expect(defaultPredicate.current.status).toBe('ready'))

    const { result: custom } = renderResource({
      isEmpty: (d) => d.items.length === 0,
    })
    await waitFor(() => expect(custom.current.status).toBe('empty'))
  })

  it('成功后 refetch 失败 → status 翻 error（失败优先于旧数据，不伪装 ready）', async () => {
    fetchList.mockResolvedValueOnce(['a'])
    const { result } = renderResource()
    await waitFor(() => expect(result.current.status).toBe('ready'))

    fetchList.mockRejectedValueOnce(new Error('刷新失败'))
    result.current.refetch()

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(result.current.error).toBe('刷新失败')
  })

  it('loading 态 refetch 可用（加载中触发重查，最终落 ready）', async () => {
    let releaseFetch: (v: string[]) => void = () => {}
    fetchList.mockImplementation(
      () => new Promise<string[]>((resolve) => { releaseFetch = resolve }),
    )
    const { result } = renderResource()

    expect(result.current.status).toBe('loading')
    result.current.refetch()
    releaseFetch(['a'])

    await waitFor(() => expect(result.current.status).toBe('ready'))
  })

  it('失败后 refetch 成功 → status 回 ready（重试钮语义）', async () => {
    fetchList.mockRejectedValueOnce(new Error('后端不可达'))
    const { result } = renderResource()
    await waitFor(() => expect(result.current.status).toBe('error'))

    fetchList.mockResolvedValueOnce(['a'])
    result.current.refetch()

    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.error).toBeNull()
  })
})
