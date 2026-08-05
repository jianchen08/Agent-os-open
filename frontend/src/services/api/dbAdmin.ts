/**
 * 统一通用数据接口 API 客户端（/api/v1/db/*）
 *
 * 表驱动、动态枚举：后端表清单/列信息由 sqlite_master + PRAGMA 运行时发现，
 * 本客户端不写死任何表名/列名——新增表/新增列自动可见、自动可查、自动可管。
 *
 * 契约对齐：.project/api_contract.md §4
 * 鉴权：复用 client.ts 统一封装（自动注入 Bearer token、统一错误处理）
 */

import apiClient from '@/services/api/client'

/** 列信息（PRAGMA table_info 行） */
export interface ColumnInfo {
  name: string
  type: string
  pk: boolean
  notnull: boolean
}

/** 表信息（/api/v1/db/tables 条目） */
export interface DbTableInfo {
  name: string
  columns: ColumnInfo[]
  row_count: number
}

/** 行查询结果（/api/v1/db/table/{table}） */
export interface DbQueryResult {
  table: string
  total: number
  limit: number
  offset: number
  rows: Record<string, unknown>[]
}

/** 行查询参数（契约 §2.2） */
export interface DbQueryParams {
  limit?: number
  offset?: number
  /** 可重复：col:eq|ne|gt|lt|contains:value，多条件 AND */
  filter?: string[]
  /** col:asc|desc，默认主键 asc */
  sort?: string
}

/** 插入结果（契约 §2.4） */
export interface DbInsertResult {
  row: Record<string, unknown>
  row_id: string
}

/** 更新结果（契约 §2.5） */
export interface DbUpdateResult {
  row: Record<string, unknown>
}

/** 删除结果（契约 §2.6） */
export interface DbDeleteResult {
  deleted: boolean
  row_id: string
}

/** SQL 执行结果（契约 §2.7） */
export interface DbExecuteResult {
  columns: string[]
  rows: unknown[][]
  rows_affected: number
}

/** 枚举全部表（名称/列/主键/行数） */
export async function fetchDbTables(): Promise<DbTableInfo[]> {
  const response = await apiClient.get<{ tables: DbTableInfo[] }>('/api/v1/db/tables')
  return response.data.tables
}

/**
 * 序列化行查询参数为 query string。
 *
 * 契约 §2.2：filter 可重复、多条件 AND。axios 默认将数组序列化为
 * `filter[]=a&filter[]=b`（参数名带 []），与后端契约字段 `filter` 不匹配，
 * 导致后端静默忽略筛选（缺陷 DEF-2）。此函数将 filter 数组序列化为
 * 重复 `filter=` 参数（`filter=a&filter=b`）。
 *
 * 导出以便单元测试断言序列化行为（回归测试防 DEF-2 复发）。
 */
export function serializeDbQueryParams(p: DbQueryParams): string {
  const sp = new URLSearchParams()
  if (p.limit !== undefined) sp.append('limit', String(p.limit))
  if (p.offset !== undefined) sp.append('offset', String(p.offset))
  if (Array.isArray(p.filter)) {
    for (const f of p.filter) sp.append('filter', f)
  }
  if (p.sort) sp.append('sort', p.sort)
  return sp.toString()
}

/** 通用行查询：分页/筛选/排序 */
export async function fetchDbRows(
  table: string,
  params: DbQueryParams = {},
): Promise<DbQueryResult> {
  const response = await apiClient.get<DbQueryResult>(`/api/v1/db/table/${table}`, {
    params,
    paramsSerializer: {
      serialize: (p) => serializeDbQueryParams(p),
    },
  })
  return response.data
}

/** 插入单行（写操作仅 admin） */
export async function insertDbRow(
  table: string,
  row: Record<string, unknown>,
): Promise<DbInsertResult> {
  const response = await apiClient.post<DbInsertResult>(`/api/v1/db/table/${table}`, {
    row,
  })
  return response.data
}

/** 更新单行（写操作仅 admin；复合主键用 `,` 拼接 pk） */
export async function updateDbRow(
  table: string,
  pk: string,
  updates: Record<string, unknown>,
): Promise<DbUpdateResult> {
  const response = await apiClient.patch<DbUpdateResult>(
    `/api/v1/db/table/${table}/${pk}`,
    { updates },
  )
  return response.data
}

/** 删除单行（写操作仅 admin；复合主键用 `,` 拼接 pk） */
export async function deleteDbRow(table: string, pk: string): Promise<DbDeleteResult> {
  const response = await apiClient.delete<DbDeleteResult>(`/api/v1/db/table/${table}/${pk}`)
  return response.data
}

/** SQL 执行器（仅 admin；SELECT 直接执行，写语句需 confirm:true） */
export async function executeDbSql(sql: string, confirm: boolean): Promise<DbExecuteResult> {
  const response = await apiClient.post<DbExecuteResult>('/api/v1/db/execute', {
    sql,
    confirm,
  })
  return response.data
}
