// @feature: FP-0.2.移动端适配 M24 DbAdminPage 排序工具栏剪裁 | @ci: frontend-test
/**
 * DbAdminPage 排序工具栏响应式契约测试
 *
 * 契约：排序列/排序方向两个 <select> 的宽度在移动端被帽住（max-w-[45vw]），
 * 不随最长列名的固有宽度撑破视口（375 实证剪裁缺陷）；桌面（md:）max-w-none
 * 保持既有行为零变化。工具栏容器允许换行与收缩（flex-wrap min-w-0 max-w-full），
 * 极端长名下两枚 select 可堆叠而不横向剪裁。
 */
import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as authApi from '@/services/api/auth'
import * as dbAdmin from '@/services/api/dbAdmin'
import { renderWithProviders } from '@/test/renderWithProviders'
import { DbAdminPage } from '../DbAdminPage'
import type { DbTableInfo } from '@/services/api/dbAdmin'

vi.mock('@/services/api/auth', () => ({
  getCurrentUser: vi.fn(),
}))

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

const adminUser = {
  id: 'admin-1',
  username: 'admin',
  email: 'admin@test.dev',
  role: 'admin',
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  last_login_at: null,
} as const

const table: DbTableInfo = {
  name: 'memory',
  columns: [
    { name: 'id', type: 'TEXT', pk: true, notnull: true },
    { name: 'payload', type: 'TEXT', pk: false, notnull: false },
  ],
  row_count: 0,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DbAdminPage — 排序工具栏响应式契约', () => {
  it('排序两枚 select 移动端帽 45vw、桌面 md:max-w-none 零变化；容器可换行收缩', async () => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockResolvedValue([table])
    mockFetchDbRows.mockResolvedValue({ table: 'memory', total: 0, limit: 50, offset: 0, rows: [] })
    const { container } = renderWithProviders(<DbAdminPage />)
    await screen.findByText('memory')

    // 排序工具栏 = 筛选栏行内的 ml-auto 容器（本修复后带 flex-wrap 收缩契约）
    const sortWrapper = container.querySelector('div.ml-auto.flex-wrap')
    expect(sortWrapper).not.toBeNull()
    expect(sortWrapper!.className).toContain('flex-wrap')
    expect(sortWrapper!.className).toContain('min-w-0')
    expect(sortWrapper!.className).toContain('max-w-full')

    const sortSelects = sortWrapper!.querySelectorAll('select')
    expect(sortSelects).toHaveLength(2)
    for (const sel of sortSelects) {
      expect(sel.className).toContain('max-w-[45vw]')
      expect(sel.className).toContain('md:max-w-none')
    }
  })

  it('分页行允许换行（375 下计数+n页+插入行不整条推出视口；桌面单行放得下零变化）', async () => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockResolvedValue([table])
    mockFetchDbRows.mockResolvedValue({ table: 'memory', total: 0, limit: 50, offset: 0, rows: [] })
    const { container } = renderWithProviders(<DbAdminPage />)
    await screen.findByText('memory')

    const pager = container.querySelector('div.flex.items-center.gap-2.border-t')
    expect(pager).not.toBeNull()
    expect(pager!.className).toContain('flex-wrap')
  })
})
