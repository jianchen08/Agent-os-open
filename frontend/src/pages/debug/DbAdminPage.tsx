/**
 * DB 管理页面（/debug/db）
 *
 * 统一通用数据接口（/api/v1/db/*）的可视化管理面板：
 * - 表列表（名称/列/行数），点击切换
 * - 数据浏览（表格渲染 + limit/offset 分页）
 * - 列筛选（eq/ne/gt/lt/contains 组合 AND）
 * - 行编辑（插入/更新/删除，删除需 confirm）
 * - SQL 调试（SELECT 直接执行；写语句二次 confirm；错误可见）
 *
 * 权限：页面仅 admin 可见（前端守卫 + 后端 403 兜底）。
 * 契约对齐：.project/api_contract.md §4 / features.md F2
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { PageShell } from '@/components/shared/PageShell'
import * as authApi from '@/services/api/auth'
import * as dbAdmin from '@/services/api/dbAdmin'
import type { ColumnInfo, DbQueryResult, DbTableInfo } from '@/services/api/dbAdmin'

/** 筛选操作符 */
const FILTER_OPS = ['eq', 'ne', 'gt', 'lt', 'contains'] as const

/** JSON 字符串美化展示（JSON 列原样展示 + 可格式化） */
function formatCell(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') {
    // JSON 列（pipeline_ids/metadata/tags 等）尝试美化
    const trimmed = value.trim()
    if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2)
      } catch {
        return value
      }
    }
    return value
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

/** 主键值拼接（复合主键用 `,`） */
function rowPk(row: Record<string, unknown>, pkCols: ColumnInfo[]): string {
  return pkCols.map((c) => String(row[c.name] ?? '')).join(',')
}

/**
 * DB 管理页面组件
 */
export function DbAdminPage() {
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null)
  const [tables, setTables] = useState<DbTableInfo[]>([])
  const [activeTable, setActiveTable] = useState<string>('')
  const [activeColumns, setActiveColumns] = useState<ColumnInfo[]>([])
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [total, setTotal] = useState(0)
  const [limit, setLimit] = useState(50)
  const [offset, setOffset] = useState(0)
  const [filters, setFilters] = useState<{ col: string; op: string; value: string }[]>([])
  const [sortCol, setSortCol] = useState('')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [isLoading, setIsLoading] = useState(true)
  const [isTableLoading, setIsTableLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  // SQL 调试
  const [sqlInput, setSqlInput] = useState('')
  const [sqlResult, setSqlResult] = useState<{ columns: string[]; rows: unknown[][] } | null>(null)
  const [sqlError, setSqlError] = useState<string | null>(null)
  const [isSqlRunning, setIsSqlRunning] = useState(false)

  /** admin 守卫：调 /api/v1/auth/me 判断角色（后端 403 兜底） */
  useEffect(() => {
    let cancelled = false
    authApi
      .getCurrentUser()
      .then((user) => {
        if (!cancelled) setIsAdmin(user.role === 'admin')
      })
      .catch(() => {
        if (!cancelled) setIsAdmin(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  /** 加载表列表 */
  const fetchTables = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const list = await dbAdmin.fetchDbTables()
      setTables(list)
      // 默认选中第一张表
      if (list.length > 0 && !activeTable) {
        setActiveTable(list[0].name)
        setActiveColumns(list[0].columns)
      }
    } catch (err) {
      setError((err as Error)?.message || '获取表列表失败')
    } finally {
      setIsLoading(false)
    }
  }, [activeTable])

  useEffect(() => {
    if (isAdmin !== true) return
    fetchTables()
  }, [fetchTables, isAdmin])

  /** 加载当前表数据 */
  const fetchRows = useCallback(async () => {
    if (!activeTable) return
    setIsTableLoading(true)
    setError(null)
    try {
      const query: DbQueryResult = await dbAdmin.fetchDbRows(activeTable, {
        limit,
        offset,
        filter: filters
          .filter((f) => f.col && f.value)
          .map((f) => `${f.col}:${f.op}:${f.value}`),
        sort: sortCol ? `${sortCol}:${sortDir}` : undefined,
      })
      setRows(query.rows)
      setTotal(query.total)
    } catch (err) {
      setError((err as Error)?.message || `查询 ${activeTable} 失败`)
      setRows([])
      setTotal(0)
    } finally {
      setIsTableLoading(false)
    }
  }, [activeTable, limit, offset, filters, sortCol, sortDir])

  useEffect(() => {
    fetchRows()
  }, [fetchRows])

  /** 切换表：重置分页/筛选/排序并加载 */
  const handleSelectTable = useCallback((table: DbTableInfo) => {
    setActiveTable(table.name)
    setActiveColumns(table.columns)
    setOffset(0)
    setFilters([])
    setSortCol('')
    setSortDir('asc')
    setMessage(null)
  }, [])

  /** 新增筛选条件 */
  const handleAddFilter = useCallback(() => {
    setFilters((prev) => [...prev, { col: activeColumns[0]?.name ?? '', op: 'eq', value: '' }])
  }, [activeColumns])

  /** 更新筛选条件 */
  const handleFilterChange = useCallback((index: number, field: 'col' | 'op' | 'value', value: string) => {
    setFilters((prev) => prev.map((f, i) => (i === index ? { ...f, [field]: value } : f)))
  }, [])

  /** 移除筛选条件 */
  const handleRemoveFilter = useCallback((index: number) => {
    setFilters((prev) => prev.filter((_, i) => i !== index))
  }, [])

  /** 插入新行（从当前行复制列结构，值留空） */
  const handleInsert = useCallback(async () => {
    if (!activeTable) return
    const newRow: Record<string, unknown> = {}
    for (const col of activeColumns) {
      if (col.pk) continue // 主键由用户填写
      newRow[col.name] = ''
    }
    const inputs = Object.entries(newRow).filter(([k]) => k !== 'tenant_id')
    const colsText = inputs.map(([k]) => `${k}:${String(newRow[k]) || '<空>'}`).join('  ')
    const ok = window.confirm(`插入新行到 ${activeTable}？\n可填列：${colsText}`)
    if (!ok) return
    try {
      // 用户输入从筛选后的可编辑表单获取（简化：提示后以空值插入，缺列用 DEFAULT）
      const row: Record<string, unknown> = {}
      for (const col of activeColumns) {
        if (col.pk) {
          const pkVal = window.prompt(`填写主键 ${col.name}（必填）`)
          if (!pkVal) return
          row[col.name] = pkVal
        } else if (col.name !== 'tenant_id') {
          const val = window.prompt(`填写 ${col.name}（留空用默认值）`) ?? ''
          if (val !== '') row[col.name] = val
        }
      }
      await dbAdmin.insertDbRow(activeTable, row)
      setMessage(`已插入 ${activeTable}`)
      setOffset(0)
      fetchRows()
    } catch (err) {
      setError((err as Error)?.message || `插入失败`)
    }
  }, [activeTable, activeColumns, fetchRows])

  /** 更新当前选中行（基于当前筛选输入，简化：提示输入） */
  const handleUpdate = useCallback(
    async (row: Record<string, unknown>, pkCols: ColumnInfo[]) => {
      if (!activeTable) return
      const pk = rowPk(row, pkCols)
      const updatable = activeColumns.filter((c) => !c.pk && c.name !== 'tenant_id')
      const sample = updatable
        .map((c) => `${c.name}:${formatCell(row[c.name]).slice(0, 20)}`)
        .join('  ')
      const newVal = window.prompt(`更新 ${activeTable}（pk=${pk}）\n可改列：${sample}\n\n输入格式 col1=值1, col2=值2`, '')
      if (!newVal) return
      const updates: Record<string, unknown> = {}
      for (const part of newVal.split(',')) {
        const idx = part.indexOf('=')
        if (idx <= 0) continue
        const key = part.slice(0, idx).trim()
        const val = part.slice(idx + 1).trim()
        if (!updatable.some((c) => c.name === key)) {
          setError(`列不存在或不可编辑: ${key}`)
          return
        }
        updates[key] = val
      }
      if (Object.keys(updates).length === 0) {
        setError('未解析到有效更新字段（格式 col1=值1, col2=值2）')
        return
      }
      try {
        await dbAdmin.updateDbRow(activeTable, pk, updates)
        setMessage(`已更新 ${activeTable} pk=${pk}`)
        fetchRows()
      } catch (err) {
        setError((err as Error)?.message || `更新失败`)
      }
    },
    [activeTable, activeColumns, fetchRows],
  )

  /** 删除当前选中行（confirm） */
  const handleDelete = useCallback(
    async (row: Record<string, unknown>, pkCols: ColumnInfo[]) => {
      if (!activeTable) return
      const pk = rowPk(row, pkCols)
      const ok = window.confirm(`确认删除 ${activeTable} 中 pk=${pk} 的行？此操作不可撤销`)
      if (!ok) return
      try {
        await dbAdmin.deleteDbRow(activeTable, pk)
        setMessage(`已删除 ${activeTable} pk=${pk}`)
        fetchRows()
      } catch (err) {
        setError((err as Error)?.message || `删除失败`)
      }
    },
    [activeTable, fetchRows],
  )

  /** 执行 SQL（写语句二次 confirm） */
  const handleRunSql = useCallback(async () => {
    const sql = sqlInput.trim()
    if (!sql) return
    const isWrite = !/^\s*(SELECT|WITH|EXPLAIN|PRAGMA\s+table_info)/i.test(sql)
    if (isWrite) {
      const ok = window.confirm(`执行写语句？\n\n${sql}\n\n确认后不可撤销（危险语句会被后端拒绝）`)
      if (!ok) return
    }
    setIsSqlRunning(true)
    setSqlError(null)
    setSqlResult(null)
    try {
      const result = await dbAdmin.executeDbSql(sql, isWrite)
      setSqlResult({ columns: result.columns, rows: result.rows })
      setMessage(`SQL 执行完成（rows_affected=${result.rows_affected}）`)
      // 若影响当前表则刷新
      if (!isWrite || /UPDATE|DELETE|INSERT/i.test(sql)) {
        fetchRows()
      }
    } catch (err) {
      setSqlError((err as Error)?.message || `SQL 执行失败`)
    } finally {
      setIsSqlRunning(false)
    }
  }, [sqlInput, fetchRows])

  /** 主键列 */
  const pkCols = useMemo(() => activeColumns.filter((c) => c.pk), [activeColumns])

  // admin 守卫
  if (isAdmin === false) {
    return (
      <PageShell title="数据库管理" backHref="/debug">
        <div className="flex h-full items-center justify-center">
          <div className="bg-destructive/10 text-destructive rounded-lg px-6 py-4 text-sm">
            无权限访问数据库管理页面（需要 admin 角色）
          </div>
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell
      title="数据库管理"
      backHref="/debug"
      actions={
        <span className="text-muted-foreground text-xs">
          {isLoading ? '加载中...' : `共 ${tables.length} 张表`}
        </span>
      }
    >
      <div className="flex h-full min-h-0 overflow-hidden">
        {/* 左：表列表 */}
        <aside className="w-56 shrink-0 overflow-y-auto border-r p-2">
          {isLoading && <div className="text-muted-foreground p-3 text-xs">加载表...</div>}
          {!isLoading && tables.length === 0 && !error && (
            <div className="text-muted-foreground p-3 text-xs">暂无表</div>
          )}
          {!isLoading &&
            tables.map((t) => (
              <button
                key={t.name}
                onClick={() => handleSelectTable(t)}
                className={`block w-full rounded-md px-3 py-2 text-left text-sm transition-colors ${
                  activeTable === t.name
                    ? 'bg-accent text-foreground'
                    : 'text-muted-foreground hover:bg-accent/50'
                }`}
              >
                <div className="font-medium">{t.name}</div>
                <div className="text-xs opacity-70">{t.row_count} 行 · {t.columns.length} 列</div>
              </button>
            ))}
        </aside>

        {/* 右：内容区 */}
        <section className="flex min-w-0 flex-1 flex-col">
          {/* 错误/消息提示 */}
          {error && (
            <div className="bg-destructive/10 text-destructive m-2 rounded-lg px-3 py-2 text-xs">
              {error}
              <button onClick={() => setError(null)} className="ml-2 underline">关闭</button>
            </div>
          )}
          {message && (
            <div className="bg-status-success/10 text-status-success m-2 rounded-lg px-3 py-2 text-xs">
              {message}
              <button onClick={() => setMessage(null)} className="ml-2 underline">关闭</button>
            </div>
          )}

          {/* 筛选栏 */}
          <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
            <span className="text-muted-foreground text-xs">筛选：</span>
            {filters.map((f, i) => (
              <div key={i} className="flex items-center gap-1">
                <select
                  value={f.col}
                  onChange={(e) => handleFilterChange(i, 'col', e.target.value)}
                  className="bg-card border-border h-7 rounded border px-1 text-xs"
                >
                  {activeColumns.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <select
                  value={f.op}
                  onChange={(e) => handleFilterChange(i, 'op', e.target.value)}
                  className="bg-card border-border h-7 rounded border px-1 text-xs"
                >
                  {FILTER_OPS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                <input
                  value={f.value}
                  onChange={(e) => handleFilterChange(i, 'value', e.target.value)}
                  placeholder="值"
                  className="bg-card border-border h-7 w-32 rounded border px-2 text-xs"
                />
                <button
                  onClick={() => handleRemoveFilter(i)}
                  className="text-muted-foreground hover:text-destructive text-xs"
                  aria-label="移除筛选"
                >
                  ✕
                </button>
              </div>
            ))}
            <button onClick={handleAddFilter} className="border-border text-muted-foreground hover:bg-accent h-7 rounded border px-2 text-xs">
              + 筛选
            </button>
            <button onClick={fetchRows} className="bg-primary text-primary-foreground h-7 rounded px-2 text-xs">
              查询
            </button>
            <div className="ml-auto flex items-center gap-1 text-xs">
              <select
                value={sortCol}
                onChange={(e) => setSortCol(e.target.value)}
                className="bg-card border-border h-7 rounded border px-1 text-xs"
              >
                <option value="">排序列</option>
                {activeColumns.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>
              <select
                value={sortDir}
                onChange={(e) => setSortDir(e.target.value as 'asc' | 'desc')}
                className="bg-card border-border h-7 rounded border px-1 text-xs"
              >
                <option value="asc">升序</option>
                <option value="desc">降序</option>
              </select>
            </div>
          </div>

          {/* 数据表格 */}
          <div className="min-h-0 flex-1 overflow-auto">
            {isTableLoading && <div className="text-muted-foreground p-4 text-xs">加载数据...</div>}
            {!isTableLoading && !error && rows.length === 0 && (
              <div className="text-muted-foreground p-6 text-center text-sm">暂无数据</div>
            )}
            {!isTableLoading && rows.length > 0 && (
              <table className="w-full text-xs">
                <thead className="bg-accent/30 sticky top-0">
                  <tr>
                    <th className="text-muted-foreground px-2 py-1.5 text-left font-medium">操作</th>
                    {activeColumns.map((c) => (
                      <th key={c.name} className="text-muted-foreground px-2 py-1.5 text-left font-medium">
                        {c.name}
                        {c.pk ? ' 🔑' : ''}
                        <span className="opacity-60"> ({c.type})</span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, idx) => (
                    <tr key={idx} className="hover:bg-accent/20 border-t align-top">
                      <td className="whitespace-nowrap px-2 py-1">
                        <button
                          onClick={() => handleUpdate(row, pkCols)}
                          className="text-status-info hover:underline"
                          title="更新此行"
                        >
                          编辑
                        </button>
                        <button
                          onClick={() => handleDelete(row, pkCols)}
                          className="text-destructive hover:underline"
                          title="删除此行"
                        >
                          删除
                        </button>
                      </td>
                      {activeColumns.map((c) => (
                        <td key={c.name} className="max-w-[240px] px-2 py-1">
                          <pre className="whitespace-pre-wrap break-all font-mono">{formatCell(row[c.name])}</pre>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* 分页 */}
          <div className="flex items-center gap-2 border-t px-3 py-2 text-xs">
            <button
              onClick={() => setOffset(Math.max(0, offset - limit))}
              disabled={offset <= 0}
              className="border-border text-muted-foreground hover:bg-accent disabled:opacity-40 rounded border px-2 py-1"
            >
              上一页
            </button>
            <span className="text-muted-foreground">
              {offset + 1}-{Math.min(offset + rows.length, total)} / 共 {total} 条
            </span>
            <button
              onClick={() => setOffset(offset + limit)}
              disabled={offset + rows.length >= total}
              className="border-border text-muted-foreground hover:bg-accent disabled:opacity-40 rounded border px-2 py-1"
            >
              下一页
            </button>
            <select
              value={limit}
              onChange={(e) => {
                setLimit(Number(e.target.value))
                setOffset(0)
              }}
              className="bg-card border-border ml-auto rounded border px-1 py-1"
            >
              {[20, 50, 100, 200, 500].map((n) => (
                <option key={n} value={n}>
                  {n}/页
                </option>
              ))}
            </select>
            <button
              onClick={handleInsert}
              className="bg-primary text-primary-foreground rounded px-2 py-1"
            >
              + 插入行
            </button>
          </div>

          {/* SQL 调试 */}
          <div className="border-t p-3">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-muted-foreground text-xs">SQL 调试（仅 admin）</span>
              {sqlError && <span className="text-destructive text-xs">{sqlError}</span>}
            </div>
            <div className="flex gap-2">
              <textarea
                value={sqlInput}
                onChange={(e) => setSqlInput(e.target.value)}
                placeholder="SELECT * FROM memory LIMIT 10&#10;写语句（UPDATE/INSERT/DELETE）需二次确认"
                rows={2}
                className="bg-card border-border min-w-0 flex-1 rounded border px-2 py-1 font-mono text-xs"
              />
              <button
                onClick={handleRunSql}
                disabled={isSqlRunning || !sqlInput.trim()}
                className="bg-accent text-foreground disabled:opacity-40 rounded px-3 py-1 text-xs"
              >
                {isSqlRunning ? '执行中...' : '执行'}
              </button>
            </div>
            {sqlResult && (
              <div className="mt-2 max-h-48 overflow-auto rounded border p-2">
                <table className="w-full text-xs">
                  <thead className="bg-accent/30">
                    <tr>
                      {sqlResult.columns.map((c, i) => (
                        <th key={i} className="text-muted-foreground px-2 py-1 text-left font-medium">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sqlResult.rows.map((r, i) => (
                      <tr key={i} className="border-t">
                        {r.map((cell, j) => (
                          <td key={j} className="px-2 py-1">
                            <pre className="whitespace-pre-wrap break-all font-mono">{formatCell(cell)}</pre>
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>
      </div>
    </PageShell>
  )
}
