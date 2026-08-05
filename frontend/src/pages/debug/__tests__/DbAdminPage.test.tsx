/**
 * DbAdminPage 组件测试
 *
 * 验证 DB 管理页面的关键行为：
 * - admin 守卫：admin 角色看到表列表；非 admin 看到无权限提示
 * - 表列表渲染：加载后显示表名与行数
 * - 数据浏览：选中表后渲染行数据
 *
 * 背景：task_01 新增 /debug/db 管理页，需确认页面消费 dbAdmin API
 * 的数据获取→状态更新→渲染链路完整，且角色守卫生效。
 */

import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as authApi from '@/services/api/auth'
import * as dbAdmin from '@/services/api/dbAdmin'
import { DbAdminPage } from '../DbAdminPage'

// mock auth API（admin 守卫用）
vi.mock('@/services/api/auth', () => ({
  getCurrentUser: vi.fn(),
}))

// mock dbAdmin API 模块
vi.mock('@/services/api/dbAdmin', () => ({
  fetchDbTables: vi.fn(),
  fetchDbRows: vi.fn(),
  insertDbRow: vi.fn(),
  updateDbRow: vi.fn(),
  deleteDbRow: vi.fn(),
  executeDbSql: vi.fn(),
}))

const mockGetCurrentUser = vi.mocked(authApi.getCurrentUser)
const mockFetchDbTables = vi.mocked(dbAdmin.fetchDbTables)
const mockFetchDbRows = vi.mocked(dbAdmin.fetchDbRows)

const sampleTables = [
  {
    name: 'memory',
    columns: [
      { name: 'id', type: 'TEXT', pk: true, notnull: true },
      { name: 'content', type: 'TEXT', pk: false, notnull: true },
      { name: 'tenant_id', type: 'TEXT', pk: false, notnull: true },
    ],
    row_count: 2,
  },
]

const sampleRows = {
  table: 'memory',
  total: 2,
  limit: 50,
  offset: 0,
  rows: [
    { id: 'm1', content: 'hello', tenant_id: 'default' },
    { id: 'm2', content: 'world', tenant_id: 'default' },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DbAdminPage 权限守卫', () => {
  it('admin 角色加载表列表并渲染', async () => {
    mockGetCurrentUser.mockResolvedValue({
      id: 'admin-1',
      username: 'admin',
      email: 'admin@test.dev',
      role: 'admin',
      is_active: true,
      created_at: '2025-01-01T00:00:00Z',
      last_login_at: null,
    })
    mockFetchDbTables.mockResolvedValue(sampleTables)
    mockFetchDbRows.mockResolvedValue(sampleRows)

    render(<DbAdminPage />)

    // 表列表渲染（表名 + 行数）
    await waitFor(() => {
      expect(screen.getByText('memory')).toBeInTheDocument()
    })
    expect(screen.getByText(/2 行 · 3 列/)).toBeInTheDocument()

    // 数据行渲染
    await waitFor(() => {
      expect(screen.getByText('hello')).toBeInTheDocument()
    })
    expect(screen.getByText('world')).toBeInTheDocument()

    // 不显示无权限
    expect(screen.queryByText(/无权限/)).not.toBeInTheDocument()
  })

  it('非 admin 角色显示无权限提示且不加载表', async () => {
    mockGetCurrentUser.mockResolvedValue({
      id: 'user-1',
      username: 'alice',
      email: 'alice@test.dev',
      role: 'user',
      is_active: true,
      created_at: '2025-01-01T00:00:00Z',
      last_login_at: null,
    })

    render(<DbAdminPage />)

    await waitFor(() => {
      expect(screen.getByText(/无权限访问数据库管理页面/)).toBeInTheDocument()
    })
    // 非 admin 不应加载表
    expect(mockFetchDbTables).not.toHaveBeenCalled()
  })
})

describe('DbAdminPage 表列表与数据浏览', () => {
  it('表加载失败时显示错误', async () => {
    mockGetCurrentUser.mockResolvedValue({
      id: 'admin-1',
      username: 'admin',
      email: 'admin@test.dev',
      role: 'admin',
      is_active: true,
      created_at: '2025-01-01T00:00:00Z',
      last_login_at: null,
    })
    mockFetchDbTables.mockRejectedValue(new Error('获取表列表失败'))

    render(<DbAdminPage />)

    await waitFor(() => {
      expect(screen.getByText(/获取表列表失败/)).toBeInTheDocument()
    })
  })
})
