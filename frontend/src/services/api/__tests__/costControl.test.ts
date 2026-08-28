// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 成本控制 API 服务测试
 *
 * 覆盖 /ext/cost_control/* 端点封装：预算状态、使用统计、成本配置、
 * 成本报表、预算重置。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as costControlApiModule from '@/services/api/costControl'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

describe('成本控制 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getBudgetStatus - 预算状态', () => {
    it('无参数时请求不带查询参数', async () => {
      const resp = {
        scope: 'global',
        limit: 1000,
        used: 100,
        remaining: 900,
        usage_percent: 10,
        alert_level: 'ok',
        estimated_cost: 0.5,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await costControlApiModule.getBudgetStatus()

      expect(result.usage_percent).toBe(10)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/cost_control/budget/status', {
        params: undefined,
      })
    })

    it('带 task_id/session_id 时透传查询参数', async () => {
      const resp = {
        scope: 'task',
        scope_id: 't1',
        limit: 100,
        used: 50,
        remaining: 50,
        usage_percent: 50,
        alert_level: 'warning',
        estimated_cost: 0.1,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await costControlApiModule.getBudgetStatus({
        task_id: 't1',
        session_id: 's1',
      })

      expect(result.scope).toBe('task')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/cost_control/budget/status', {
        params: { task_id: 't1', session_id: 's1' },
      })
    })
  })

  describe('getUsageStatistics - 使用统计', () => {
    it('请求统计端点并解包', async () => {
      const resp = {
        global_stats: {
          daily_tokens: 1,
          monthly_tokens: 2,
          daily_limit: 3,
          monthly_limit: 4,
          daily_usage_percent: 5,
          monthly_usage_percent: 6,
          estimated_daily_cost: 0.1,
          estimated_monthly_cost: 0.2,
        },
        tasks: [],
        sessions: [],
        recent_records: [],
        updated_at: 't',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await costControlApiModule.getUsageStatistics()

      expect(result.global_stats.daily_tokens).toBe(1)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/cost_control/usage/statistics')
    })
  })

  describe('getCostConfig - 成本配置', () => {
    it('请求配置端点并解包', async () => {
      const resp = {
        daily_token_limit: 1000,
        monthly_token_limit: 10000,
        per_task_token_limit: 100,
        per_session_token_limit: 100,
        warning_threshold: 0.8,
        critical_threshold: 0.95,
        auto_save_at_warning: true,
        auto_pause_at_critical: true,
        auto_stop_at_exhausted: true,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await costControlApiModule.getCostConfig()

      expect(result.warning_threshold).toBe(0.8)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/cost_control/config')
    })
  })

  describe('getCostReport - 成本报表', () => {
    it('无参数时请求不带查询参数', async () => {
      const resp = {
        period: 'daily',
        start_date: 'd1',
        end_date: 'd2',
        total_tokens: 0,
        total_cost: 0,
        by_model: {},
        by_task: {},
        daily_breakdown: [],
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await costControlApiModule.getCostReport()

      expect(result.period).toBe('daily')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/cost_control/report', {
        params: undefined,
      })
    })

    it('带 period 时透传查询参数', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({}))

      await costControlApiModule.getCostReport({ period: 'monthly' })

      expect(apiClient.get).toHaveBeenCalledWith('/ext/cost_control/report', {
        params: { period: 'monthly' },
      })
    })
  })

  describe('resetBudget - 预算重置', () => {
    it('POST 重置端点并解包', async () => {
      const resp = { message: '预算已重置' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(resp))

      const result = await costControlApiModule.resetBudget({ session_id: 's1' })

      expect(result.message).toBe('预算已重置')
      expect(apiClient.post).toHaveBeenCalledWith('/ext/cost_control/budget/reset', null, {
        params: { session_id: 's1' },
      })
    })
  })
})
