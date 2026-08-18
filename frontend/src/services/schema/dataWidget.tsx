/**
 * dataWidget —— 数据 widget 数据获取（全面 widget 化计划 A1a，2026-08-18）
 *
 * - `datasourceUri` 语义与 fetchDatasourceOptions 一致：以 `/` 开头=绝对 URI
 *   直连；否则走 `/api/v1/datasource/{*}` 代理（G6-a 真实路由已通）。
 * - 数据形状协议（shape）：`rows`（表格 {columns,rows}）/ `series`（图表
 *   {labels,datasets}）/ `scalar`（状态卡 {value|metrics|progress}）。
 *   chart/table/status_card 三个数据 widget 消费本层；与 RefreshBox(poll)
 *   组合 = 声明周期重挂载即重拉；WS 推送由 A1c（useWsDataSource）承接
 *   （事件驱动更新，不走 remount）。
 * - 无 uri 时回退静态 props（零行为变化，兼容旧声明）。
 */
import { useEffect, useState } from 'react'
import apiClient from '@/services/api/client'

export type DataShape = 'rows' | 'series' | 'scalar'

export interface DataWidgetResult {
  data: unknown
  loading: boolean
  error: string | null
}

/** 取原始 payload（解 `{data: ...}` 信封一层；其余原样） */
async function fetchDatasourcePayload(uri: string): Promise<unknown> {
  const url = uri.startsWith('/') ? uri : `/api/v1/datasource/${uri}`
  const resp = await apiClient.get(url)
  const d: unknown = resp.data
  if (
    d &&
    typeof d === 'object' &&
    !Array.isArray(d) &&
    !('columns' in (d as object)) &&
    !('labels' in (d as object)) &&
    'data' in (d as object)
  ) {
    return (d as { data: unknown }).data
  }
  return d
}

// ── 数据形状归一化 ─────────────────────────────────────────

export interface DataColumn {
  key: string
  label: string
  [k: string]: unknown
}

/** rows：{columns/rows} 或裸数组 → 标准表形；无 columns 时取首行 key 生成 */
export function normalizeRows(payload: unknown): {
  columns: DataColumn[]
  rows: Record<string, unknown>[]
} {
  const raw = payload as Record<string, unknown> | unknown[] | null | undefined
  if (Array.isArray(raw)) {
    return buildRowsFromArray(raw)
  }
  if (raw && typeof raw === 'object') {
    if (Array.isArray(raw.rows)) {
      return {
        columns: Array.isArray(raw.columns)
          ? raw.columns.filter(
              (c): c is DataColumn =>
                !!c && typeof c === 'object' && typeof (c as DataColumn).key === 'string',
            )
          : inferColumns(raw.rows as Record<string, unknown>[]),
        rows: raw.rows.filter(
          (r): r is Record<string, unknown> => !!r && typeof r === 'object',
        ),
      }
    }
    if (Array.isArray(raw.data)) {
      return buildRowsFromArray(raw.data as unknown[])
    }
  }
  return { columns: [], rows: [] }
}

function buildRowsFromArray(arr: unknown[]): { columns: DataColumn[]; rows: Record<string, unknown>[] } {
  const rows = arr.filter(
    (r): r is Record<string, unknown> => !!r && typeof r === 'object' && !Array.isArray(r),
  )
  return { columns: inferColumns(rows), rows }
}

function inferColumns(rows: Record<string, unknown>[]): DataColumn[] {
  const keys = rows.length > 0 ? Object.keys(rows[0] ?? {}) : []
  return keys.map((key) => ({ key, label: key }))
}

export interface ChartDataset {
  data: number[]
  label?: string
  color?: string
  backgroundColor?: string
}

/** series：{labels,datasets} 或信封内嵌 → 标准图数据；裸数组 -> 单序列 */
export function normalizeSeries(payload: unknown): {
  labels: string[]
  datasets: ChartDataset[]
} {
  const raw = payload as Record<string, unknown> | unknown[] | null | undefined
  if (Array.isArray(raw)) {
    const nums = raw.map(Number).filter((n) => Number.isFinite(n))
    return {
      labels: nums.map((_, i) => String(i)),
      datasets: nums.length > 0 ? [{ data: nums }] : [],
    }
  }
  if (raw && typeof raw === 'object' && Array.isArray(raw.datasets)) {
    const labels = Array.isArray(raw.labels) ? raw.labels.map(String) : []
    const datasets = (raw.datasets as unknown[])
      .filter((d) => !!d && typeof d === 'object' && Array.isArray((d as { data?: unknown }).data))
      .map((d) => {
        const ds = d as ChartDataset
        return {
          data: ds.data.map(Number),
          label: ds.label,
          color: ds.color,
          backgroundColor: ds.backgroundColor,
        }
      })
    return { labels, datasets }
  }
  return { labels: [], datasets: [] }
}

/** scalar：解信封后的原对象或值（status_card 读 value/metrics/progress） */
export function normalizeScalar(payload: unknown): Record<string, unknown> {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    return payload as Record<string, unknown>
  }
  return { value: payload }
}

export function normalizeDataPayload(payload: unknown, shape: DataShape): unknown {
  switch (shape) {
    case 'rows':
      return normalizeRows(payload)
    case 'series':
      return normalizeSeries(payload)
    case 'scalar':
      return normalizeScalar(payload)
    default:
      return payload
  }
}

// ── hook ───────────────────────────────────────────────────

/**
 * 数据 widget 取数 hook：datasourceUri（HTTP 拉，A1a）→ 归一化数据。
 * 无 uri 时回退静态 data/value（零行为变化）。
 */
export function useDataWidget(props: Record<string, unknown>, shape: DataShape): DataWidgetResult {
  const uri = props.datasourceUri as string | undefined
  const staticData = props.data ?? props.value
  const [state, setState] = useState<DataWidgetResult>({
    data: staticData,
    loading: false,
    error: null,
  })

  useEffect(() => {
    if (!uri) {
      setState({ data: staticData, loading: false, error: null })
      return
    }
    let cancelled = false
    setState({ data: staticData, loading: true, error: null })
    fetchDatasourcePayload(uri)
      .then((payload) => {
        if (!cancelled) {
          setState({ data: normalizeDataPayload(payload, shape), loading: false, error: null })
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            data: staticData,
            loading: false,
            error: err instanceof Error ? err.message : '数据加载失败',
          })
        }
      })
    return () => {
      cancelled = true
    }
    // staticData 对象每次渲染引用会变——只依赖 uri/shape，避免拉取循环
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uri, shape])

  return state
}

/** 展示层小样：加载/错误提示（数据 widget 通用） */
export function DataWidgetStatus({
  loading,
  error,
}: {
  loading: boolean
  error: string | null
}) {
  if (loading) {
    return <p className="text-muted-foreground py-2 text-center text-xs">加载数据…</p>
  }
  if (error) {
    return (
      <p className="text-status-error py-2 text-center text-xs" role="alert">
        {error}
      </p>
    )
  }
  return null
}
