/** @feature FP-T12 前端适配(会话列表缓存通道) | @ci: frontend-test */
/**
 * useSessionsQuery 缓存通道——ensureSessionsLoaded（缓存命中零请求/未命中
 * 拉取回写）与 forceReloadSessions（无视新鲜度强制重拉回写）。getSessions
 * 为外部 API 依赖走 mock，queryClient 真实例。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/services/api/session', () => ({ getSessions: vi.fn() }))

import { getSessions } from '@/services/api/session'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import {
  ensureSessionsLoaded,
  forceReloadSessions,
  readSessions,
  updateSessionsCache,
} from '@/hooks/queries/useSessionsQuery'

describe('ensureSessionsLoaded / forceReloadSessions — 缓存通道', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    queryClient.clear()
  })

  afterEach(() => {
    queryClient.clear()
  })

  it('ensure：缓存命中零请求原样返回；未命中拉取一次并回写缓存', async () => {
    updateSessionsCache(() => [{ id: 's1' } as never])
    vi.mocked(getSessions).mockResolvedValue([{ id: 's2' } as never])
    const hit = await ensureSessionsLoaded()
    expect(hit).toEqual([{ id: 's1' }])
    expect(getSessions).not.toHaveBeenCalled()

    queryClient.clear()
    const miss = await ensureSessionsLoaded()
    expect(miss).toEqual([{ id: 's2' }])
    expect(getSessions).toHaveBeenCalledTimes(1)
    expect(readSessions()).toEqual([{ id: 's2' }])
  })

  it('forceReload：新鲜缓存也强制重拉并覆盖回写（管道跳转兜底语义）', async () => {
    updateSessionsCache(() => [{ id: 'stale' } as never])
    vi.mocked(getSessions).mockResolvedValue([{ id: 'fresh' } as never])
    const fresh = await forceReloadSessions()
    expect(fresh).toEqual([{ id: 'fresh' }])
    expect(getSessions).toHaveBeenCalledTimes(1)
    expect(queryClient.getQueryData(queryKeys.sessions)).toEqual([{ id: 'fresh' }])
  })

  it('API 返回 null 归一空数组（不发明数据）', async () => {
    vi.mocked(getSessions).mockResolvedValue(null as never)
    await expect(forceReloadSessions()).resolves.toEqual([])
  })
})
