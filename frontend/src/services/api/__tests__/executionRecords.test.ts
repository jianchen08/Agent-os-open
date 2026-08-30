// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * Execution Records API 服务测试
 *
 * 覆盖 /ext/monitoring/execution/records* 端点封装：列表、会话列表、
 * 单条记录（失败降级 null）、子记录（空数据兜底 []）、清空（破坏性操作）。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as executionRecordsApi from '@/services/api/executionRecords'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

describe('Execution Records API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getExecutionRecords - 列表', () => {
    it('默认参数请求', async () => {
      const resp = { records: [], total: 0 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.getExecutionRecords()

      expect(result).toEqual(resp)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/execution/records', {
        params: {},
      })
    })

    it('透传 session_id/parent_record_id/limit/offset', async () => {
      const resp = {
        records: [{ id: 'r1', session_id: 's1', message_data: {}, created_at: 't' }],
        total: 1,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.getExecutionRecords({
        session_id: 's1',
        parent_record_id: 'p1',
        limit: 10,
        offset: 5,
      })

      expect(result.records).toHaveLength(1)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/execution/records', {
        params: { session_id: 's1', parent_record_id: 'p1', limit: 10, offset: 5 },
      })
    })
  })

  describe('getExecutionRecordsSessions - 会话列表', () => {
    it('请求会话列表端点', async () => {
      const resp = {
        sessions: [
          { id: 's1', title: 't', created_at: 'c', updated_at: 'u', record_count: 3 },
        ],
        total: 1,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.getExecutionRecordsSessions()

      expect(result.sessions[0].record_count).toBe(3)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/execution/records/sessions')
    })
  })

  describe('clearAllExecutionRecords - 清空', () => {
    it('POST 清空端点并返回响应', async () => {
      const resp = { success: true, message: 'cleared', cleared_count: 9 }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.clearAllExecutionRecords()

      expect(result).toEqual(resp)
      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/monitoring/execution/records/clear-all',
      )
    })
  })
})
