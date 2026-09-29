// @feature: FP-T12 前端适配(应用壳/内核内存指标 API) | @ci: frontend-test
/**
 * systemMetrics API 服务测试（全口径内存内核段数据源）
 *
 * apiClient 是外部依赖（网络层），用可控 mock 替换并断言请求契约；
 * 返回值映射断言可观察输出（process_rss_bytes 缺失/非法兜底 null）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const getMock = vi.fn()

vi.mock('../client', () => {
  const mockClient = {
    get: (...args: unknown[]) => getMock(...args),
  }
  return { default: mockClient, apiClient: mockClient }
})

import { getKernelMemStats } from '../systemMetrics'

beforeEach(() => {
  getMock.mockReset()
})

describe('getKernelMemStats', () => {
  it('GET /api/v1/system/memstats，process_rss_bytes 原样透传', async () => {
    getMock.mockResolvedValue({ data: { process_rss_bytes: 104857600 } })

    await expect(getKernelMemStats()).resolves.toEqual({ process_rss_bytes: 104857600 })
    expect(getMock).toHaveBeenCalledWith('/api/v1/system/memstats')
  })

  it('process_rss_bytes 为 null（内核采集失败）→ 透传 null 不猜测', async () => {
    getMock.mockResolvedValue({ data: { process_rss_bytes: null } })

    await expect(getKernelMemStats()).resolves.toEqual({ process_rss_bytes: null })
  })

  it('响应体缺 process_rss_bytes 字段 → 兜底 null', async () => {
    getMock.mockResolvedValue({ data: {} })

    await expect(getKernelMemStats()).resolves.toEqual({ process_rss_bytes: null })
  })
})
