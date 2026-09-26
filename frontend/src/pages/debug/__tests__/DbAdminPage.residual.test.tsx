// @feature: FP-T12 DbAdminPage 补测 | @ci: frontend-test
/**
 * DbAdminPage 残余分支补测（簇3，与 .test / .branches 互补）
 *
 * 覆盖既有测试未触达的分支：
 * - formatCell 的六条取值路径：null/undefined → 空、JSON 字符串美化、
 *   形似 JSON 但解析失败 → 原文、对象 → JSON.stringify、不可序列化对象
 *   （循环引用） → String(value)、其他标量 → String(value)
 * - admin 守卫的 catch 分支（getCurrentUser 拒绝 → 判为无权限）
 * - 筛选条件下拉切换列（col 维度的 onChange）
 *
 * mock 仅限外部服务（auth/dbAdmin API）与浏览器宿主 API（confirm/prompt）。
 * formatCell 未导出，经表格单元格渲染间接驱动（断行为：渲染文本）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as authApi from '@/services/api/auth'
import * as dbAdmin from '@/services/api/dbAdmin'
import { renderWithProviders } from '@/test/renderWithProviders'
import { DbAdminPage } from '../DbAdminPage'
import type { DbQueryParams, DbTableInfo } from '@/services/api/dbAdmin'

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
    { name: 'meta', type: 'JSON', pk: false, notnull: false },
    { name: 'count', type: 'INTEGER', pk: false, notnull: false },
  ],
  row_count: 1,
}

beforeEach(() => {
  vi.clearAllMocks()
})

/** 渲染 admin 视图并把行数据换成给定行 */
async function renderWithRows(rows: Record<string, unknown>[]): Promise<void> {
  mockGetCurrentUser.mockResolvedValue(adminUser)
  mockFetchDbTables.mockResolvedValue([table])
  mockFetchDbRows.mockResolvedValue({
    table: 'memory',
    total: rows.length,
    limit: 50,
    offset: 0,
    rows,
  })
  renderWithProviders(<DbAdminPage />)
  await screen.findByText('memory')
  // 表格区就绪（表头列名出现即说明 rows 已渲染）
  await screen.findByRole('columnheader', { name: /payload/ })
}

/** 表格正文全部单元格文本 */
function cellTexts(): string[] {
  const body = document.querySelector('tbody')
  if (!body) return []
  return Array.from(body.querySelectorAll('pre')).map((p) => p.textContent ?? '')
}

describe('formatCell 取值路径', () => {
  it('JSON 字符串列 → 美化输出（缩进换行，非原文一行）', async () => {
    await renderWithRows([{ id: 'r1', payload: '{"a":1,"b":[2,3]}' }])

    const texts = cellTexts()
    const json = texts.find((t) => t.includes('"a"'))
    expect(json).toBeDefined()
    // 美化后含换行与缩进（原始紧凑串不含换行）
    expect(json).toContain('\n')
    expect(json?.split('\n').length).toBeGreaterThan(1)
  })

  it('数组形 JSON 字符串 → 同样美化', async () => {
    await renderWithRows([{ id: 'r2', payload: '[1,2,3]' }])
    const json = cellTexts().find((t) => t.includes('1'))
    expect(json).toContain('\n')
  })

  it.each([
    ['以 { 开头且以 } 结尾但内容非法 JSON', '{not: valid}'],
    ['以 [ 开头且以 ] 结尾但内容非法 JSON', '[1, 2,]'],
  ])('形似 JSON（%s）但解析失败 → 回落原文（不丢内容、不抛错）', async (_name, broken) => {
    await renderWithRows([{ id: 'r3', payload: broken }])
    expect(cellTexts()).toContain(broken)
  })

  it('对象列 → JSON.stringify 美化；其他标量 → String(value)', async () => {
    await renderWithRows([{ id: 'r4', meta: { nested: { deep: true } }, count: 42 }])

    const texts = cellTexts()
    expect(texts.some((t) => t.includes('"nested"') && t.includes('\n'))).toBe(true)
    expect(texts).toContain('42')
  })

  it('布尔与 null 值：null → 空串、布尔 → String 形态', async () => {
    await renderWithRows([{ id: 'r5', payload: null, count: true }])
    const texts = cellTexts()
    // null 渲染为空 pre（无文本）
    expect(texts).toContain('')
    expect(texts).toContain('true')
  })

  it('循环引用对象 → String(value) 兜底不崩（JSON.stringify 抛错被捕获）', async () => {
    const cyclic: Record<string, unknown> = { name: 'loop' }
    cyclic.self = cyclic
    await renderWithRows([{ id: 'r6', meta: cyclic }])

    // 渲染未崩溃，且该单元格以 String(value) 形态出现
    expect(screen.getByRole('columnheader', { name: /meta/ })).toBeInTheDocument()
    expect(cellTexts().some((t) => t.includes('[object Object]'))).toBe(true)
  })

  it.each([
    ['纯文本', 'plain text value', 'plain text value'],
    ['数字字符串', '123', '123'],
    ['空串', '', ''],
  ])('%s 单元格原样展示', async (_name, value, expected) => {
    await renderWithRows([{ id: 'r7', payload: value }])
    expect(cellTexts()).toContain(expected)
  })
})

describe('admin 守卫失败分支', () => {
  it('getCurrentUser 拒绝 → 判为无权限，不请求表列表', async () => {
    mockGetCurrentUser.mockRejectedValue(new Error('401 unauthorized'))
    renderWithProviders(<DbAdminPage />)

    expect(await screen.findByText(/无权限访问数据库管理页面/)).toBeInTheDocument()
    expect(mockFetchDbTables).not.toHaveBeenCalled()
  })

  it('非 admin 角色 → 无权限提示（守卫按 role 判定）', async () => {
    mockGetCurrentUser.mockResolvedValue({ ...adminUser, role: 'user' })
    renderWithProviders(<DbAdminPage />)

    expect(await screen.findByText(/无权限访问数据库管理页面/)).toBeInTheDocument()
  })

  it('embedded 模式无权限态 → 仍渲染权限提示（外壳不同不改变守卫）', async () => {
    mockGetCurrentUser.mockResolvedValue({ ...adminUser, role: 'user' })
    renderWithProviders(<DbAdminPage embedded />)

    expect(await screen.findByText(/无权限访问数据库管理页面/)).toBeInTheDocument()
  })
})

describe('筛选列切换', () => {
  it('切换筛选列（col 维度）→ 查询参数以新列名发出', async () => {
    await renderWithRows([{ id: 'r8', payload: 'p' }])

    fireEvent.click(screen.getByRole('button', { name: '+ 筛选' }))
    // 默认列 = activeColumns[0] = id
    const valueInput = screen.getByPlaceholderText('值')

    // 排序列下拉同样含列名选项，须限定在筛选行内（值输入框的兄弟 select）
    const filterRow = valueInput.closest('.flex.items-center.gap-1') as HTMLElement
    const [colSelect, opSelect] = Array.from(
      filterRow.querySelectorAll('select'),
    ) as HTMLSelectElement[]
    expect(colSelect.value).toBe('id')
    expect(opSelect.value).toBe('eq')

    fireEvent.change(colSelect, { target: { value: 'payload' } })
    fireEvent.change(valueInput, { target: { value: 'needle' } })

    await waitFor(() => {
      const calls = mockFetchDbRows.mock.calls
      const params = calls[calls.length - 1][1] as DbQueryParams
      expect(params.filter).toEqual(['payload:eq:needle'])
    })
  })

  it('多条筛选条件各自独立切换（第二条改动不影响第一条）', async () => {
    await renderWithRows([{ id: 'r9', payload: 'p' }])

    fireEvent.click(screen.getByRole('button', { name: '+ 筛选' }))
    fireEvent.click(screen.getByRole('button', { name: '+ 筛选' }))
    const valueInputs = screen.getAllByPlaceholderText('值')
    expect(valueInputs).toHaveLength(2)

    fireEvent.change(valueInputs[0], { target: { value: 'first' } })
    fireEvent.change(valueInputs[1], { target: { value: 'second' } })

    await waitFor(() => {
      const calls = mockFetchDbRows.mock.calls
      const params = calls[calls.length - 1][1] as DbQueryParams
      expect(params.filter).toEqual(['id:eq:first', 'id:eq:second'])
    })
  })
})
