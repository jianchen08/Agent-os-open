/** @feature FP-0.2.二 内部模块manifest @vision V6 可即用 @ci frontend-test */
/**
 * 监控 API 测试 —— F-MON-1：active_requests 真实化 + 前端展示
 *
 * 测试 /ext/monitoring/token-usage 端点返回的新字段（active_requests /
 * error_count / total_response_time）能被 getTokenUsage 正确解包透传，
 * 供 MonitoringPage 渲染实际活跃 LLM 请求数。
 *
 * WHY：F-MON-1 之前 active_requests 恒为 0（后端未配对维护，前端也未消费）。
 * 修复后后端 _collect_token_usage 暴露真实值，前端 TokenUsage 类型扩展可选字段，
 * 此测试钉死「API 新字段 → 前端透传」链路，防止回归。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as monitoringApi from '@/services/api/monitoring'
import type { TokenUsage } from '@/types/monitoring'

// Mock axios client
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({
  data,
  status: 200,
  statusText: 'OK',
  headers: {},
  config: {} as any,
})

describe('监控 API - active_requests 真实化（F-MON-1）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getTokenUsage - 透传 active_requests/error_count/total_response_time', () => {
    it('应解包 token_usage 并保留 active_requests 等新字段', async () => {
      const tokenUsage: TokenUsage = {
        total_tokens: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        request_count: 3,
        active_requests: 2,
        error_count: 1,
        total_response_time: 4.5,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ token_usage: tokenUsage }))

      const result = await monitoringApi.getTokenUsage()

      expect(result.active_requests).toBe(2)
      expect(result.error_count).toBe(1)
      expect(result.total_response_time).toBe(4.5)
      expect(result.request_count).toBe(3)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/token-usage')
    })

    it('后端未返回新字段时应兼容（字段 undefined，前端以 ?? 0 兜底）', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        okResponse({
          token_usage: {
            total_tokens: 0,
            prompt_tokens: 0,
            completion_tokens: 0,
            request_count: 1,
          },
        }),
      )

      const result = await monitoringApi.getTokenUsage()

      // 新字段可选：旧后端不返回时为 undefined，不破坏现有契约
      expect(result.request_count).toBe(1)
      expect(result.active_requests).toBeUndefined()
      expect(result.error_count).toBeUndefined()
    })
  })

  describe('getAllMonitoringData - tokenUsage 含活跃请求数', () => {
    it('汇总接口应透传 tokenUsage.active_requests', async () => {
      vi.mocked(apiClient.get).mockImplementation(async (url: string) => {
        if (url === '/ext/monitoring/token-usage') {
          return okResponse({
            token_usage: {
              total_tokens: 0,
              prompt_tokens: 0,
              completion_tokens: 0,
              request_count: 5,
              active_requests: 3,
              error_count: 0,
              total_response_time: 10,
            },
          }) as any
        }
        if (url === '/ext/monitoring/system/metrics') {
          return okResponse({
            metrics: {
              cpu_usage: 10,
              memory: { total: 1, used: 1, available: 1, usage_percent: 1 },
              disk: { mount_point: '/', total: 1, used: 1, free: 1, usage_percent: 1 },
              uptime: 1,
              timestamp: 't',
            },
          }) as any
        }
        if (url === '/ext/monitoring/tasks/statistics') {
          return okResponse({
            statistics: {
              total: 0,
              succeeded: 0,
              failed: 0,
              running: 0,
              pending: 0,
              success_rate: 0,
            },
          }) as any
        }
        if (url === '/ext/monitoring/tasks') {
          return okResponse({ items: [], total: 0, page: 1, page_size: 20 }) as any
        }
        if (url === '/ext/monitoring/cache-stats') {
          return okResponse({
            cache_stats: { cache_hits: 0, cache_misses: 0, hit_rate: 0, total_requests: 0 },
          }) as any
        }
        return okResponse({}) as any
      })

      const data = await monitoringApi.getAllMonitoringData()

      expect(data.tokenUsage).not.toBeNull()
      expect(data.tokenUsage?.active_requests).toBe(3)
      expect(data.tokenUsage?.error_count).toBe(0)
      expect(data.tokenUsage?.total_response_time).toBe(10)
    })
  })
})
