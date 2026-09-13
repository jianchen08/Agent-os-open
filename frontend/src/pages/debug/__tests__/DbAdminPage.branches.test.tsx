// @feature: FP-T12 DbAdminPage 补测 | @ci: frontend-test
/**
 * DbAdminPage 分支补测
 *
 * 既有 DbAdminPage.test.tsx 覆盖权限守卫与主链渲染；本文件补齐其余分支：
 * - 表列表空态/加载态/错误兜底文案（非 Error 拒因）
 * - 行查询失败分支（Error / 非 Error）与错误条关闭
 * - 筛选（操作符枚举 / 移除）与排序（升/降）、分页（下一页/上一页/每页条数）
 * - 切表重置分页、活跃表被删后回落第一张
 * - 行编辑确认流：插入（confirm/prompt 链）、更新（输入解析合法/非法）、删除
 * - SQL 调试：空白禁执行、SELECT 直执行、写语句二次确认取消/确认、执行失败
 *
 * mock 仅限外部服务（auth/dbAdmin API）与浏览器宿主 API（confirm/prompt）；
 * 断言全部落在可观察行为：渲染输出与 API 调用入参。
 *
 * 同步纪律：点击前等加载链沉降（React19 act 环境下与在途微任务链竞争会吞
 * 后续续体），点击后以空 act 冲刷点击触发的异步链再断言。
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as authApi from '@/services/api/auth'
import * as dbAdmin from '@/services/api/dbAdmin'
import type { DbQueryParams, DbTableInfo } from '@/services/api/dbAdmin'
import { queryKeys } from '@/services/query/queryKeys'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import { DbAdminPage } from '../DbAdminPage'

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
const mockInsertDbRow = vi.mocked(dbAdmin.insertDbRow)
const mockUpdateDbRow = vi.mocked(dbAdmin.updateDbRow)
const mockDeleteDbRow = vi.mocked(dbAdmin.deleteDbRow)
const mockExecuteDbSql = vi.mocked(dbAdmin.executeDbSql)

const adminUser = {
  id: 'admin-1',
  username: 'admin',
  email: 'admin@test.dev',
  role: 'admin',
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  last_login_at: null,
} as const

const tables: DbTableInfo[] = [
  {
    name: 'memory',
    columns: [
      { name: 'id', type: 'TEXT', pk: true, notnull: true },
      { name: 'content', type: 'TEXT', pk: false, notnull: true },
      { name: 'tenant_id', type: 'TEXT', pk: false, notnull: false },
    ],
    row_count: 2,
  },
  {
    name: 'tasks',
    columns: [
      { name: 'id', type: 'TEXT', pk: true, notnull: true },
      { name: 'status', type: 'TEXT', pk: false, notnull: false },
    ],
    row_count: 5,
  },
]

const memoryPage: dbAdmin.DbQueryResult = {
  table: 'memory',
  total: 4,
  limit: 50,
  offset: 0,
  rows: [
    { id: 'm1', content: 'hello', tenant_id: 'default' },
    { id: 'm2', content: 'world', tenant_id: 'default' },
  ],
}

// confirm/prompt 为浏览器宿主 API，按外部依赖 spy，afterEach 统一还原
const hostSpies: Array<ReturnType<typeof vi.spyOn>> = []
function spyConfirm(ret: boolean): ReturnType<typeof vi.spyOn> {
  const spy = vi.spyOn(window, 'confirm').mockReturnValue(ret)
  hostSpies.push(spy)
  return spy
}
function spyPrompt(...returns: (string | null)[]): ReturnType<typeof vi.spyOn> {
  const spy = vi.spyOn(window, 'prompt').mockImplementation(() => returns.shift() ?? null)
  hostSpies.push(spy)
  return spy
}
afterEach(() => {
  while (hostSpies.length > 0) {
    const spy = hostSpies.pop()
    spy?.mockRestore()
  }
})

beforeEach(() => {
  vi.clearAllMocks()
})

/** 冲刷点击触发的异步链（fetch/写操作 promise 续体）再断言 */
async function flushAsync(): Promise<void> {
  await act(async () => {})
}

/** admin 登录 + 两张表 + memory 行页（total=4 制造可翻页场景），渲染并等列表与行数据沉降 */
async function renderAdmin(): Promise<void> {
  mockGetCurrentUser.mockResolvedValue(adminUser)
  mockFetchDbTables.mockResolvedValue(tables)
  mockFetchDbRows.mockResolvedValue(structuredClone(memoryPage))
  renderWithProviders(<DbAdminPage />)
  await screen.findByText('memory')
  await screen.findByText('hello')
}

/** 最近一次行查询的参数 */
function lastRowCall(): DbQueryParams {
  const calls = mockFetchDbRows.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[calls.length - 1][1]
}

/** 按选项文案反查所属 select（避免依赖 combobox 的 DOM 顺序） */
function selectContainingOption(label: string): HTMLSelectElement {
  const select = screen.getByRole('option', { name: label }).closest('select')
  if (!(select instanceof HTMLSelectElement)) {
    throw new Error(`找不到选项 ${label} 所属的 select`)
  }
  return select
}

describe('表列表：空态 / 加载态 / 错误兜底', () => {
  it('无表时显示空态（暂无表 + 暂无数据），且不请求行数据', async () => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockResolvedValue([])
    renderWithProviders(<DbAdminPage />)

    expect(await screen.findByText('暂无表')).toBeInTheDocument()
    expect(screen.getByText('暂无数据')).toBeInTheDocument()
    expect(screen.getByText('共 0 张表')).toBeInTheDocument()
    expect(mockFetchDbRows).not.toHaveBeenCalled()
  })

  it('表列表加载中显示加载态（列表占位 + 顶部计数）', () => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockReturnValue(new Promise<DbTableInfo[]>(() => {}))
    renderWithProviders(<DbAdminPage />)

    expect(screen.getByText('加载表...')).toBeInTheDocument()
    expect(screen.getByText('加载中...')).toBeInTheDocument()
  })

  it.each([
    ['Error 拒因透传 message', new Error('backend 500'), 'backend 500'],
    ['非 Error 拒因落到兜底文案', 'oops', '获取表列表失败'],
  ])('表列表失败显示错误（%s）', async (_name, rejectWith, expected) => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockRejectedValue(rejectWith)
    renderWithProviders(<DbAdminPage />)

    expect(await screen.findByText(expected)).toBeInTheDocument()
  })
})

describe('行数据加载与错误分支', () => {
  it('初始行查询以默认分页参数发出', async () => {
    await renderAdmin()
    await waitFor(() =>
      expect(mockFetchDbRows).toHaveBeenCalledWith('memory', {
        limit: 50,
        offset: 0,
        filter: [],
        sort: undefined,
      }),
    )
  })

  it.each([
    ['Error 拒因透传 message', new Error('row query boom'), 'row query boom'],
    ['非 Error 拒因落到兜底文案', undefined, '查询 memory 失败'],
  ])('行查询失败显示错误条（%s）', async (_name, rejectWith, expected) => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockResolvedValue(tables)
    mockFetchDbRows.mockRejectedValue(rejectWith)
    renderWithProviders(<DbAdminPage />)

    expect(await screen.findByText(expected)).toBeInTheDocument()
    // 错误态下不显示空态占位
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()
  })

  it('错误条可通过关闭按钮清除，清除后回到空态占位', async () => {
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockResolvedValue(tables)
    mockFetchDbRows.mockRejectedValue(new Error('boom'))
    renderWithProviders(<DbAdminPage />)

    await screen.findByText('boom')
    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    expect(screen.queryByText('boom')).not.toBeInTheDocument()
    expect(screen.getByText('暂无数据')).toBeInTheDocument()
  })
})

describe('筛选 / 排序 / 分页参数提交', () => {
  it.each([
    ['eq', 'id:eq:abc'],
    ['contains', 'id:contains:abc'],
  ])('筛选操作符 %s 随输入自动提交为 filter 参数，移除后清空', async (op, expectedFilter) => {
    await renderAdmin()

    fireEvent.click(screen.getByRole('button', { name: '+ 筛选' }))
    fireEvent.change(selectContainingOption(op), { target: { value: op } })
    fireEvent.change(screen.getByPlaceholderText('值'), { target: { value: 'abc' } })
    await flushAsync()

    expect(lastRowCall().filter).toEqual([expectedFilter])

    fireEvent.click(screen.getByRole('button', { name: '移除筛选' }))
    await flushAsync()
    expect(lastRowCall().filter).toEqual([])
  })

  it('排序：选列默认升序，切降序后 sort 参数跟随', async () => {
    await renderAdmin()

    fireEvent.change(selectContainingOption('排序列'), { target: { value: 'content' } })
    await flushAsync()
    expect(lastRowCall().sort).toBe('content:asc')

    fireEvent.change(selectContainingOption('降序'), { target: { value: 'desc' } })
    await flushAsync()
    expect(lastRowCall().sort).toBe('content:desc')
  })

  it('分页：下一页发出 offset=limit 且末页禁用，上一页回到 offset=0', async () => {
    await renderAdmin()

    const prev = screen.getByRole('button', { name: '上一页' })
    const next = screen.getByRole('button', { name: '下一页' })
    expect(prev).toBeDisabled()
    expect(screen.getByText('1-2 / 共 4 条')).toBeInTheDocument()

    fireEvent.click(next)
    await flushAsync()
    expect(lastRowCall().offset).toBe(50)
    // 同一页数据（rows=2, total=4）下 offset(50)+2 >= 4 → 末页禁用
    expect(next).toBeDisabled()

    fireEvent.click(prev)
    await flushAsync()
    expect(lastRowCall().offset).toBe(0)
  })

  it('切换每页条数：以新 limit 回到第一页', async () => {
    await renderAdmin()

    fireEvent.change(selectContainingOption('20/页'), { target: { value: '20' } })
    await flushAsync()
    expect(lastRowCall().limit).toBe(20)
    expect(lastRowCall().offset).toBe(0)
  })

  it('点击查询按钮重新发起当前表的行查询', async () => {
    await renderAdmin()
    const callsBefore = mockFetchDbRows.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '查询' }))
    await flushAsync()
    expect(mockFetchDbRows.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(lastRowCall().limit).toBe(50)
  })
})

describe('切表与回落', () => {
  it('切换表：以新表名从第一页加载并更新表头', async () => {
    await renderAdmin()

    fireEvent.click(screen.getByText('tasks'))
    await flushAsync()
    expect(mockFetchDbRows).toHaveBeenLastCalledWith('tasks', expect.objectContaining({ offset: 0 }))
    expect(screen.getByRole('columnheader', { name: /status/ })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: /content/ })).not.toBeInTheDocument()
  })

  it('翻页后切换表：分页重置回 offset=0', async () => {
    await renderAdmin()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await flushAsync()
    expect(lastRowCall().offset).toBe(50)

    fireEvent.click(screen.getByText('tasks'))
    await flushAsync()
    expect(mockFetchDbRows).toHaveBeenLastCalledWith('tasks', expect.objectContaining({ offset: 0 }))
  })

  it('表列表刷新后当前表不存在：回落选中第一张表', async () => {
    const client = createTestQueryClient()
    mockGetCurrentUser.mockResolvedValue(adminUser)
    mockFetchDbTables.mockResolvedValue(tables)
    mockFetchDbRows.mockResolvedValue(structuredClone(memoryPage))
    renderWithProviders(<DbAdminPage />, { queryClient: client })
    await screen.findByText('memory')
    await screen.findByText('hello')
    fireEvent.click(screen.getByText('tasks'))
    await flushAsync()
    await screen.findByRole('columnheader', { name: /status/ })

    // tasks 被删除后的表列表刷新
    mockFetchDbTables.mockResolvedValue([tables[0]])
    await client.refetchQueries({ queryKey: queryKeys.dbTables })

    await waitFor(() =>
      expect(mockFetchDbRows).toHaveBeenLastCalledWith('memory', expect.objectContaining({ offset: 0 })),
    )
    expect(screen.getByRole('columnheader', { name: /content/ })).toBeInTheDocument()
  })
})

describe('插入行确认流', () => {
  it('confirm 取消：不弹主键输入、不调用插入 API', async () => {
    await renderAdmin()
    const confirmSpy = spyConfirm(false)
    const promptSpy = spyPrompt()

    fireEvent.click(screen.getByRole('button', { name: '+ 插入行' }))

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('插入新行到 memory'))
    expect(promptSpy).not.toHaveBeenCalled()
    expect(mockInsertDbRow).not.toHaveBeenCalled()
  })

  it('confirm 确认但主键输入取消：不调用插入 API', async () => {
    await renderAdmin()
    spyConfirm(true)
    const promptSpy = spyPrompt(null)

    fireEvent.click(screen.getByRole('button', { name: '+ 插入行' }))

    expect(promptSpy).toHaveBeenCalledTimes(1)
    expect(mockInsertDbRow).not.toHaveBeenCalled()
  })

  it('confirm + 填写主键与非主键列：提交行数据（不含 tenant_id）并回到第一页刷新', async () => {
    await renderAdmin()
    spyConfirm(true)
    spyPrompt('m9', 'hi') // 依次：id 主键 → content；tenant_id 跳过

    fireEvent.click(screen.getByRole('button', { name: '+ 插入行' }))
    await flushAsync()

    expect(mockInsertDbRow).toHaveBeenCalledWith('memory', { id: 'm9', content: 'hi' })
    expect(await screen.findByText('已插入 memory')).toBeInTheDocument()
    expect(mockFetchDbRows).toHaveBeenLastCalledWith('memory', expect.objectContaining({ offset: 0 }))
  })

  it('非主键列输入取消：该列不提交（走后端默认值）', async () => {
    await renderAdmin()
    spyConfirm(true)
    spyPrompt('m9', null)

    fireEvent.click(screen.getByRole('button', { name: '+ 插入行' }))
    await flushAsync()

    expect(mockInsertDbRow).toHaveBeenCalledWith('memory', { id: 'm9' })
  })

  it('插入失败：错误条显示后端消息', async () => {
    await renderAdmin()
    spyConfirm(true)
    spyPrompt('m9', 'x')
    mockInsertDbRow.mockRejectedValue(new Error('UNIQUE constraint failed'))

    fireEvent.click(screen.getByRole('button', { name: '+ 插入行' }))
    await flushAsync()

    expect(await screen.findByText('UNIQUE constraint failed')).toBeInTheDocument()
  })
})

describe('更新行输入解析', () => {
  function clickFirstRowEdit(): void {
    fireEvent.click(screen.getAllByRole('button', { name: '编辑' })[0])
  }

  it('prompt 取消：不调用更新 API', async () => {
    await renderAdmin()
    spyPrompt(null)

    clickFirstRowEdit()

    expect(mockUpdateDbRow).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('合法输入 col=值：按 pk 提交更新并显示成功消息', async () => {
    await renderAdmin()
    spyPrompt('content=新值')

    clickFirstRowEdit()
    await flushAsync()

    expect(mockUpdateDbRow).toHaveBeenCalledWith('memory', 'm1', { content: '新值' })
    expect(await screen.findByText('已更新 memory pk=m1')).toBeInTheDocument()
  })

  it.each([
    ['列不存在', 'nope=1', '列不存在或不可编辑: nope'],
    ['无有效字段', ',,,', '未解析到有效更新字段（格式 col1=值1, col2=值2）'],
  ])('非法输入（%s）：显示解析错误且不提交', async (_name, input, expectedError) => {
    await renderAdmin()
    spyPrompt(input)

    clickFirstRowEdit()
    await flushAsync()

    expect(await screen.findByText(expectedError)).toBeInTheDocument()
    expect(mockUpdateDbRow).not.toHaveBeenCalled()
  })

  it('更新失败：错误条显示后端消息', async () => {
    await renderAdmin()
    spyPrompt('content=x')
    mockUpdateDbRow.mockRejectedValue(new Error('db locked'))

    clickFirstRowEdit()
    await flushAsync()

    expect(await screen.findByText('db locked')).toBeInTheDocument()
  })
})

describe('删除行确认流', () => {
  it('confirm 取消：不调用删除 API，且确认文案含不可撤销警示', async () => {
    await renderAdmin()
    const confirmSpy = spyConfirm(false)

    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0])

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('不可撤销'))
    expect(mockDeleteDbRow).not.toHaveBeenCalled()
  })

  it('confirm 确认：按 pk 删除并显示成功消息，消息可关闭', async () => {
    await renderAdmin()
    spyConfirm(true)

    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0])
    await flushAsync()

    expect(mockDeleteDbRow).toHaveBeenCalledWith('memory', 'm1')
    expect(await screen.findByText('已删除 memory pk=m1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    expect(screen.queryByText('已删除 memory pk=m1')).not.toBeInTheDocument()
  })

  it('删除失败：错误条显示后端消息', async () => {
    await renderAdmin()
    spyConfirm(true)
    mockDeleteDbRow.mockRejectedValue(new Error('foreign key constraint'))

    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0])
    await flushAsync()

    expect(await screen.findByText('foreign key constraint')).toBeInTheDocument()
  })
})

describe('SQL 调试', () => {
  function typeSql(sql: string): void {
    fireEvent.change(screen.getByPlaceholderText(/SELECT \* FROM memory/), { target: { value: sql } })
  }

  it.each(['', '   '])('SQL 输入为空白（"%s"）时执行按钮禁用', async (sql) => {
    await renderAdmin()

    const runBtn = screen.getByRole('button', { name: '执行' })
    expect(runBtn).toBeDisabled()
    if (sql !== '') {
      typeSql(sql)
      expect(runBtn).toBeDisabled()
    }
  })

  it('SELECT 直接执行（confirm=false）并渲染结果与完成消息', async () => {
    await renderAdmin()
    mockExecuteDbSql.mockResolvedValue({ columns: ['id'], rows: [['r1']], rows_affected: 0 })

    typeSql('SELECT id FROM memory')
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    await flushAsync()

    expect(mockExecuteDbSql).toHaveBeenCalledWith('SELECT id FROM memory', false)
    expect(await screen.findByText('r1')).toBeInTheDocument()
    expect(screen.getByText('SQL 执行完成（rows_affected=0）')).toBeInTheDocument()
  })

  it('写语句 confirm 取消：不执行，确认文案含语句原文与警示', async () => {
    await renderAdmin()
    const confirmSpy = spyConfirm(false)

    typeSql('UPDATE memory SET content = 1')
    fireEvent.click(screen.getByRole('button', { name: '执行' }))

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('UPDATE memory SET content = 1'))
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('不可撤销'))
    expect(mockExecuteDbSql).not.toHaveBeenCalled()
  })

  it('写语句 confirm 确认：以 confirm=true 执行并显示影响行数', async () => {
    await renderAdmin()
    spyConfirm(true)
    mockExecuteDbSql.mockResolvedValue({ columns: [], rows: [], rows_affected: 2 })

    typeSql('DELETE FROM memory')
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    await flushAsync()

    expect(mockExecuteDbSql).toHaveBeenCalledWith('DELETE FROM memory', true)
    expect(await screen.findByText('SQL 执行完成（rows_affected=2）')).toBeInTheDocument()
  })

  it('SQL 执行失败：错误显示且不出结果', async () => {
    await renderAdmin()
    spyConfirm(true)
    mockExecuteDbSql.mockRejectedValue(new Error('危险语句被拒绝'))

    typeSql('DROP TABLE memory')
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    await flushAsync()

    expect(await screen.findByText('危险语句被拒绝')).toBeInTheDocument()
    expect(screen.queryByText(/SQL 执行完成/)).not.toBeInTheDocument()
  })
})
