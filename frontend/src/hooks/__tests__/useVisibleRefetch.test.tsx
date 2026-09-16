// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * useVisibleRefetch 契约测试
 *
 * 仅 false→true 跳变触发 invalidate；挂载（初值 true）与持续可见不触发。
 */
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { QueryClient, QueryKey } from '@tanstack/react-query'
import { useVisibleRefetch } from '../useVisibleRefetch'

function makeClient() {
  return {
    invalidateQueries: vi.fn(),
  } as unknown as QueryClient
}

const KEYS: QueryKey[] = [['a'], ['b']]

describe('useVisibleRefetch', () => {
  it('挂载（初值可见）不触发 invalidate', () => {
    const client = makeClient()
    renderHook(() => useVisibleRefetch(true, KEYS, client))
    expect(client.invalidateQueries).not.toHaveBeenCalled()
  })

  it('不可见期间不触发；恢复可见对每个 key invalidate 一次', () => {
    const client = makeClient()
    const { rerender } = renderHook(
      ({ visible }: { visible: boolean }) => useVisibleRefetch(visible, KEYS, client),
      { initialProps: { visible: true } },
    )

    rerender({ visible: false })
    rerender({ visible: false })
    expect(client.invalidateQueries).not.toHaveBeenCalled()

    rerender({ visible: true })
    expect(client.invalidateQueries).toHaveBeenCalledTimes(2)
    expect(client.invalidateQueries).toHaveBeenNthCalledWith(1, { queryKey: ['a'] })
    expect(client.invalidateQueries).toHaveBeenNthCalledWith(2, { queryKey: ['b'] })

    // 持续可见不重复触发
    rerender({ visible: true })
    expect(client.invalidateQueries).toHaveBeenCalledTimes(2)
  })

  it('多次离屏/恢复循环，每次恢复各触发一轮', () => {
    const client = makeClient()
    const { rerender } = renderHook(
      ({ visible }: { visible: boolean }) => useVisibleRefetch(visible, [['k']], client),
      { initialProps: { visible: true } },
    )
    for (let i = 0; i < 3; i++) {
      rerender({ visible: false })
      rerender({ visible: true })
    }
    expect(client.invalidateQueries).toHaveBeenCalledTimes(3)
  })
})
