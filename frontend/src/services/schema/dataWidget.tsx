/**
 * dataWidget —— 数据 widget 数据获取（全面 widget 化计划 A1a）
 *
 * - `datasourceUri` 语义与 fetchDatasourceOptions 一致：以 `/` 开头=绝对 URI
 *   直连；否则走 `/api/v1/datasource/{*}` 代理（G6-a 真实路由已通）。
 * - 数据形状协议（shape）：`rows`（表格 {columns,rows}）/ `series`（图表
 *   {labels,datasets}）/ `scalar`（状态卡 {value|metrics|progress}）。
 *   chart/table/status_card 三个数据 widget 消费本层；与 RefreshBox(poll)
 *   组合 = 声明周期重挂载即重拉；WS 推送由 `refresh:{type:'ws',channel}`
 *   （A1c）承接——事件驱动更新，不走 remount。
 * - 无 uri/ws 时回退静态 props（零行为变化，兼容旧声明）。
 */
import { useEffect, useRef, useState } from 'react'
import apiClient from '@/services/api/client'
import { globalWS } from '@/services/websocket/GlobalWebSocket'

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

/**
 * uri → 最近一次成功载荷（模块级）。数据在挂载时获取的组件（槽位轮询递增
 * key、切标签重建宿主）每次重挂载都会重放"拉取中→空窗"：先渲上次成功载荷
 * 再静默刷新，重挂载不再闪空。
 */
const lastPayloadByUri = new Map<string, unknown>()

/**
 * 同 URI 取数合并：仅合并**并发窗口内**的重复请求。
 *
 * 声明式页面里同一 `datasourceUri` 常被多个 widget 消费（监控页资源组 3 卡同
 * URI、用量与成本组多表多图同 URI），各 widget 独立 effect 会在同刻发出重复
 * 请求。此处按 URI 合并：同刻在飞的请求共享同一次调用，后到者复用其结果。
 *
 * 不做时间窗缓存：显式重拉（reloadKey 递增 = 轮询/行操作刷新）必须真打后端取
 * 新值，任何 TTL 复用都会让用户看到过期数据。请求一旦落地即从表中移除，下一个
 * 拉取（含轮询 tick）照常发新请求。
 */
const inFlightByUri = new Map<string, Promise<unknown>>()

/** 清空共享取数状态（测试用：模块级缓存在用例间会串） */
export function resetSharedFetchCache(): void {
  inFlightByUri.clear()
  lastPayloadByUri.clear()
}

function fetchShared(uri: string): Promise<unknown> {
  const running = inFlightByUri.get(uri)
  if (running) return running
  const task = fetchDatasourcePayload(uri).finally(() => {
    inFlightByUri.delete(uri)
  })
  inFlightByUri.set(uri, task)
  return task
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
    if (Array.isArray(raw.items)) {
      // {items, total} 信封（如 monitoring 插件 tasks）
      return buildRowsFromArray(raw.items as unknown[])
    }
    if (Array.isArray(raw.results)) {
      // {results, total} 信封（如 hindsight_memory_service recall）
      return buildRowsFromArray(raw.results as unknown[])
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

/** 声明 WS 推送源（A1c）：refresh:{type:'ws', channel} */
export interface WsRefreshConfig {
  type: 'ws'
  channel: string
}

/** 从组件 props 解析 WS 推送声明；非 ws/缺 channel → null */
function parseWsRefresh(props: Record<string, unknown>): WsRefreshConfig | null {
  const r = props.refresh
  if (!r || typeof r !== 'object') return null
  const cfg = r as Record<string, unknown>
  if (cfg.type !== 'ws') return null
  const channel = cfg.channel
  return typeof channel === 'string' && channel !== '' ? { type: 'ws', channel } : null
}

/**
 * 数据 widget 取数 hook：三种数据源按声明择一——
 * - `refresh:{type:'ws',channel}`：WS 推送，事件驱动更新（A1c）；
 * - `datasourceUri`：HTTP 拉，归一化（A1a）；
 * - 均无：静态 data/value（零行为变化）。
 * reloadKey：外部触发重拉（如表格行操作成功后），变化即重新取数。
 * visible：宿主面板可见性（useElementVisible）——不可见时 WS 推送不再触发
 * setState/重渲染（隐藏面板的重渲染纯浪费），仅缓存最新一帧，恢复可见即应用；
 * HTTP 侧由 RefreshBox 冻结 reloadKey 承担，此处不重复治理。默认 true（
 * 未接可见性的调用方行为不变）。
 */
export function useDataWidget(
  props: Record<string, unknown>,
  shape: DataShape,
  reloadKey = 0,
  visible = true,
): DataWidgetResult {
  const uri = props.datasourceUri as string | undefined
  const staticData = props.data ?? props.value
  const ws = parseWsRefresh(props)
  const [state, setState] = useState<DataWidgetResult>(() => ({
    data:
      uri && lastPayloadByUri.has(uri)
        ? normalizeDataPayload(lastPayloadByUri.get(uri), shape)
        : staticData,
    loading: false,
    error: null,
  }))
  /** WS handler 运行时读（避免闭包过期 + 不因 visible 翻转重订阅） */
  const visibleRef = useRef(visible)
  useEffect(() => {
    visibleRef.current = visible
  }, [visible])
  /** 离屏期间最新一帧 WS 载荷（恢复可见时应用，数据不丢只延迟） */
  const pendingWsPayloadRef = useRef<{ payload: unknown } | null>(null)

  useEffect(() => {
    // WS 事件驱动（A1c）：事件即数据，shape 归一后更新，不走 loading
    if (ws) {
      const handler = (payload: unknown) => {
        if (!visibleRef.current) {
          pendingWsPayloadRef.current = { payload }
          return
        }
        setState((prev) => ({
          ...prev,
          data: normalizeDataPayload(payload, shape),
          loading: false,
          error: null,
        }))
      }
      globalWS.subscribe(ws.channel, handler)
      return () => globalWS.unsubscribe(ws.channel, handler)
    }
    if (!uri) {
      setState({ data: staticData, loading: false, error: null })
      return
    }
    let cancelled = false
    // 轮询重拉（reloadKey 变化）时保留已渲染数据：有旧值不置 loading、
    // 不闪"加载中"占位（只有首挂载无数据才显示加载态）
    setState((prev) => ({
      data: prev.data ?? staticData,
      loading: prev.data == null,
      error: null,
    }))
    fetchShared(uri)
      .then((payload) => {
        if (!cancelled) {
          lastPayloadByUri.set(uri, payload)
          setState({ data: normalizeDataPayload(payload, shape), loading: false, error: null })
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState((prev) => ({
            // 刷新失败保留已渲染数据（避免整卡回退静态空值闪变）
            data: prev.data ?? staticData,
            loading: false,
            error: err instanceof Error ? err.message : '数据加载失败',
          }))
        }
      })
    return () => {
      cancelled = true
    }
    // staticData 对象每次渲染引用会变——只依赖关键源，避免拉取循环
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uri, shape, ws?.channel, reloadKey])

  /** 恢复可见：应用离屏期间缓存的最新一帧 WS 载荷（无缓存则空操作） */
  useEffect(() => {
    const pending = pendingWsPayloadRef.current
    if (!visible || !ws || !pending) return
    pendingWsPayloadRef.current = null
    setState((prev) => ({
      ...prev,
      data: normalizeDataPayload(pending.payload, shape),
      loading: false,
      error: null,
    }))
  }, [visible, ws, shape])

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
