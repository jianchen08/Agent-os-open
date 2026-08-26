/**
 * Execution Records API 服务测试
 *
 * 覆盖 /ext/monitoring/execution/records* 端点封装：分组概要、列表、会话列表、
 * 单条记录（失败降级 null）、记录树、子记录（空数据兜底 []）、清空（破坏性操作）。
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

  describe('getRecordGroupSummary - 分组概要', () => {
    it('不带 sessionId 时请求无查询参数', async () => {
      const resp = { groups: [], total_groups: 0 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.getRecordGroupSummary()

      expect(result).toEqual(resp)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/monitoring/execution/records/group-summary',
        { params: {} },
      )
    })

    it('带 sessionId 时透传查询参数', async () => {
      const resp = {
        groups: [{ parent_record_id: 'p1', record_count: 2, earliest_time: null }],
        total_groups: 1,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.getRecordGroupSummary('s1')

      expect(result.total_groups).toBe(1)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/monitoring/execution/records/group-summary',
        { params: { session_id: 's1' } },
      )
    })
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

  describe('getExecutionRecord - 单条记录', () => {
    it('成功时返回记录', async () => {
      const rec = { id: 'r1', session_id: 's1', message_data: {}, created_at: 't' }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(rec))

      const result = await executionRecordsApi.getExecutionRecord('r1')

      expect(result).toEqual(rec)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/monitoring/execution/records/r1')
    })

    it('请求失败时降级返回 null 且不抛异常', async () => {
      vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('Network Error'))
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      const result = await executionRecordsApi.getExecutionRecord('r1')

      expect(result).toBeNull()
      consoleSpy.mockRestore()
    })
  })

  describe('getExecutionTree - 记录树', () => {
    it('默认 maxDepth=5', async () => {
      const resp = { tree: [], total: 0, session_id: 's1', max_depth: 5 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await executionRecordsApi.getExecutionTree('s1')

      expect(result.max_depth).toBe(5)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/monitoring/execution/records/tree/s1',
        { params: { max_depth: 5 } },
      )
    })

    it('自定义 maxDepth', async () => {
      const resp = { tree: [], total: 0, session_id: 's1', max_depth: 3 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      await executionRecordsApi.getExecutionTree('s1', 3)

      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/monitoring/execution/records/tree/s1',
        { params: { max_depth: 3 } },
      )
    })
  })

  describe('getChildrenRecords - 子记录', () => {
    it('返回子记录数组', async () => {
      const children = [{ id: 'c1', session_id: 's1', message_data: {}, created_at: 't' }]
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(children))

      const result = await executionRecordsApi.getChildrenRecords('p1')

      expect(result).toEqual(children)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/monitoring/execution/records/p1/children',
      )
    })

    it('后端返回空时兜底为空数组', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(null))

      const result = await executionRecordsApi.getChildrenRecords('p1')

      expect(result).toEqual([])
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
