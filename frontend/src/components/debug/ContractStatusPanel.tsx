/**
 * ContractStatusPanel —— 插件契约状态面板（闸2·观测前端，配合
 * 《插件契约校验闸门体系完整方案》§5.3：并入调试中心）。
 *
 * 消费 GET /api/v1/plugins/contract-status（每插件一条契约状态）：
 *   { plugin_id, enabled,
 *     gates: { manifest_schema_valid, dep_ok, g2_consistency, smoke_result,
 *              render_decl_valid, runtime_input_violations,
 *              runtime_output_violations, last_error },
 *     last_scan_ts }
 * 端点未实现/404 → 降级提示（后端上线即通，前端协议已对齐）。
 */
import { useEffect, useState } from 'react'
import apiClient from '@/services/api/client'

export interface ContractGateState {
  manifest_schema_valid?: boolean
  dep_ok?: boolean
  g2_consistency?: 'ok' | 'drift' | 'not_covered' | string
  smoke_result?: 'ok' | 'failed' | 'skipped' | string
  render_decl_valid?: 'ok' | 'invalid' | 'not_declared' | string
  runtime_input_violations?: number
  runtime_output_violations?: number
  last_error?: string
}

export interface PluginContractStatus {
  plugin_id: string
  enabled?: boolean
  gates?: ContractGateState
  last_scan_ts?: string
}

/** 红灯判定：任一高等级闸失败 → 红灯（与方案"红灯即某个闸失败"一致） */
export function contractRedLight(status: PluginContractStatus): boolean {
  const g = status.gates
  if (!g) return false
  if (g.manifest_schema_valid === false) return true
  if (g.dep_ok === false) return true
  if (g.g2_consistency === 'drift') return true
  if (g.smoke_result === 'failed') return true
  if (g.render_decl_valid === 'invalid') return true
  return false
}

/** 契约状态解析（兼容数组 / {plugins:[...]} / {items:[...]} 信封） */
export function parseContractStatus(raw: unknown): PluginContractStatus[] {
  if (Array.isArray(raw)) return raw as PluginContractStatus[]
  if (raw && typeof raw === 'object') {
    const rec = raw as Record<string, unknown>
    if (Array.isArray(rec.plugins)) return rec.plugins as PluginContractStatus[]
    if (Array.isArray(rec.items)) return rec.items as PluginContractStatus[]
  }
  return []
}

export function ContractStatusPanel() {
  const [statuses, setStatuses] = useState<PluginContractStatus[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'unavailable'>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setState('loading')
    apiClient
      .get('/api/v1/plugins/contract-status')
      .then((resp) => {
        if (cancelled) return
        const list = parseContractStatus(resp.data)
        setStatuses(list)
        setState(list.length > 0 ? 'ready' : 'ready')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState('unavailable')
        setError(err instanceof Error ? err.message : '端点不可用')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (state === 'loading') {
    return <p className="text-muted-foreground py-6 text-center text-sm">加载契约状态…</p>
  }

  if (state === 'unavailable') {
    return (
      <div className="py-6 text-center">
        <p className="text-status-error text-sm" role="alert">
          契约状态端点不可用（{error}）
        </p>
        <p className="text-muted-foreground mt-1 text-xs">
          等待内核 GET /api/v1/plugins/contract-status 上线（契约校验闸2·观测）
        </p>
      </div>
    )
  }

  if (statuses.length === 0) {
    return <p className="text-muted-foreground py-6 text-center text-sm">暂无插件契约状态</p>
  }

  return (
    <div className="space-y-1.5">
      {statuses.map((s) => {
        const red = contractRedLight(s)
        const g = s.gates ?? {}
        return (
          <div
            key={s.plugin_id}
            data-testid={`contract-status-${s.plugin_id}`}
            data-red={red}
            className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
              red
                ? 'border-status-error/40 bg-status-error/5'
                : 'border-border/50 bg-muted/20'
            }`}
          >
            <span
              className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${
                red ? 'bg-status-error' : 'bg-status-success'
              }`}
              data-testid={`contract-light-${s.plugin_id}`}
            />
            <span className="font-mono text-xs font-medium">{s.plugin_id}</span>
            {s.enabled === false && (
              <span className="text-muted-foreground rounded bg-muted px-1.5 py-0.5 text-[10px]">
                已禁用
              </span>
            )}
            <span className="text-muted-foreground ml-auto flex items-center gap-2 font-mono text-[10px]">
              <span>G2:{g.g2_consistency ?? 'n/a'}</span>
              <span>冒烟:{g.smoke_result ?? 'n/a'}</span>
              <span>入参:{g.runtime_input_violations ?? 0}</span>
              <span>出参:{g.runtime_output_violations ?? 0}</span>
            </span>
            {g.last_error && (
              <span className="text-status-error hidden max-w-[240px] truncate text-[10px] lg:inline">
                {g.last_error}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
