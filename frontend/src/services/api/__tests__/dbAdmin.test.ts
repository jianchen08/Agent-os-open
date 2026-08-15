/**
 * dbAdmin API 客户端测试
 *
 * 验证统一数据接口客户端的请求构造（URL/方法/参数/请求体）与响应解包，
 * 对齐 .project/api_contract.md §4 契约。
 *
 * 背景：task_01 新增 /ext/db_admin/* 统一数据接口 + 前端 DB 管理页，
 * 需确认 dbAdmin.ts 的每个函数正确映射到后端端点。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as dbAdmin from '@/services/api/dbAdmin'

// 用 vi.hoisted 创建 mock 函数，确保在 vi.mock 提升时可用
const { mockGet, mockPost, mockPatch, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockDelete: vi.fn(),
}))

// Mock client 模块 — dbAdmin.ts 使用 default 导出 apiClient
vi.mock('../client', () => ({
  default: {
    get: mockGet,
    post: mockPost,
    patch: mockPatch,
    delete: mockDelete,
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('dbAdmin API 客户端', () => {
  it('fetchDbTables 请求 GET /ext/db_admin/tables 并返回表数组', async () => {
    mockGet.mockResolvedValue({
      data: {
        tables: [
          {
            name: 'memory',
            columns: [{ name: 'id', type: 'TEXT', pk: true, notnull: true }],
            row_count: 3,
          },
        ],
      },
    })

    const result = await dbAdmin.fetchDbTables()

    expect(mockGet).toHaveBeenCalledWith('/ext/db_admin/tables')
    expect(result).toHaveLength(1)
    expect(result[0].name).toBe('memory')
    expect(result[0].columns[0].pk).toBe(true)
    expect(result[0].row_count).toBe(3)
  })

  it('fetchDbRows 请求 GET /ext/db_admin/table/{table} 并携带分页/筛选/排序参数', async () => {
    mockGet.mockResolvedValue({
      data: {
        table: 'memory',
        total: 2,
        limit: 50,
        offset: 0,
        rows: [{ id: 'm1', content: 'hello' }],
      },
    })

    const result = await dbAdmin.fetchDbRows('memory', {
      limit: 10,
      offset: 20,
      filter: ['memory_type:eq:episode', 'content:contains:hello'],
      sort: 'created_at:desc',
    })

    expect(mockGet).toHaveBeenCalledWith('/ext/db_admin/table/memory', {
      params: {
        limit: 10,
        offset: 20,
        filter: ['memory_type:eq:episode', 'content:contains:hello'],
        sort: 'created_at:desc',
      },
      // 契约 §2.2：filter 可重复参数（DEF-2 修复：axios 默认 filter[] 与后端不匹配）
      paramsSerializer: expect.any(Object),
    })
    expect(result.total).toBe(2)
    expect(result.rows[0].id).toBe('m1')
  })

  it('serializeDbQueryParams 将 filter 数组序列化为重复 filter= 参数（契约 §2.2）', () => {
    const qs = dbAdmin.serializeDbQueryParams({
      limit: 10,
      offset: 20,
      filter: ['memory_type:eq:episode', 'content:contains:hello'],
      sort: 'created_at:desc',
    })
    // 断言重复 filter=（而非 axios 默认 filter[]=）——DEF-2 根因
    expect(qs).toContain('filter=memory_type%3Aeq%3Aepisode')
    expect(qs).toContain('filter=content%3Acontains%3Ahello')
    expect(qs).toContain('limit=10')
    expect(qs).toContain('offset=20')
    expect(qs).toContain('sort=created_at%3Adesc')
    // 不得出现 filter[] 形式（与后端契约字段不匹配）
    expect(qs).not.toContain('filter%5B%5D')
    expect(qs).not.toContain('filter[]')
  })

  it('fetchDbRows 无参数时仅传表名', async () => {
    mockGet.mockResolvedValue({ data: { table: 'memory', total: 0, limit: 50, offset: 0, rows: [] } })

    await dbAdmin.fetchDbRows('memory')

    expect(mockGet).toHaveBeenCalledWith('/ext/db_admin/table/memory', {
      params: {},
      paramsSerializer: expect.any(Object),
    })
  })

  it('insertDbRow 请求 POST /ext/db_admin/table/{table} 携带 row 并返回 row/row_id', async () => {
    mockPost.mockResolvedValue({
      data: {
        row: { id: 'new1', content: 'x', tenant_id: 'default' },
        row_id: 'new1',
      },
    })

    const result = await dbAdmin.insertDbRow('memory', { id: 'new1', content: 'x' })

    expect(mockPost).toHaveBeenCalledWith('/ext/db_admin/table/memory', {
      row: { id: 'new1', content: 'x' },
    })
    expect(result.row_id).toBe('new1')
    expect(result.row.content).toBe('x')
  })

  it('updateDbRow 请求 PATCH /ext/db_admin/table/{table}/{pk} 携带 updates', async () => {
    mockPatch.mockResolvedValue({
      data: { row: { id: 'm1', content: 'updated' } },
    })

    const result = await dbAdmin.updateDbRow('memory', 'm1', { content: 'updated' })

    expect(mockPatch).toHaveBeenCalledWith('/ext/db_admin/table/memory/m1', {
      updates: { content: 'updated' },
    })
    expect(result.row.content).toBe('updated')
  })

  it('deleteDbRow 请求 DELETE /ext/db_admin/table/{table}/{pk}', async () => {
    mockDelete.mockResolvedValue({ data: { deleted: true, row_id: 'm1' } })

    const result = await dbAdmin.deleteDbRow('memory', 'm1')

    expect(mockDelete).toHaveBeenCalledWith('/ext/db_admin/table/memory/m1')
    expect(result.deleted).toBe(true)
  })

  it('executeDbSql 请求 POST /ext/db_admin/execute 携带 sql 与 confirm', async () => {
    mockPost.mockResolvedValue({
      data: {
        columns: ['id', 'content'],
        rows: [['m1', 'hello']],
        rows_affected: 0,
      },
    })

    const result = await dbAdmin.executeDbSql('SELECT * FROM memory', true)

    expect(mockPost).toHaveBeenCalledWith('/ext/db_admin/execute', {
      sql: 'SELECT * FROM memory',
      confirm: true,
    })
    expect(result.columns).toEqual(['id', 'content'])
    expect(result.rows[0]).toEqual(['m1', 'hello'])
    expect(result.rows_affected).toBe(0)
  })
})
