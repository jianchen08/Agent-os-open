/**
 * 成本看板 Widget · Deep Space v2
 *
 * 设计来源：画布 C · WorkspacePanel · 看板内容 (49:277)
 * - 统计卡行：今日 / 本周消耗
 * - 近 7 日 Token 柱状
 * - 模型用量占比条
 *
 * 数据：对接 /api/v1/cost-control/*（useCostControl）
 */

import { useEffect, useMemo } from 'react'
import { useCostControl } from '@/hooks/useCostControl'
import { cn } from '@/lib/utils'

function formatTokens(n: number): string {
  if (!Number.isFinite(n)) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(Math.round(n))
}

function formatCny(usd: number): string {
  // 后端 estimated_cost 为美元量级；展示用 ¥ 近似（设计稿人民币）
  const cny = Number.isFinite(usd) ? usd * 7.2 : 0
  if (cny < 0.01) return `¥${cny.toFixed(4)}`
  return `¥${cny.toFixed(2)}`
}

function dayLabel(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso.slice(5, 10) || iso
    return `${d.getMonth() + 1}/${d.getDate()}`
  } catch {
    return iso
  }
}

/**
 * 成本看板 — Workspace 页签内容
 */
export function CostDashboardWidget(_props: Record<string, unknown>) {
  const {
    usageStats,
    costReport,
    isLoading,
    error,
    fetchUsageStatistics,
    fetchCostReport,
  } = useCostControl()

  useEffect(() => {
    void fetchUsageStatistics().catch(() => {})
    void fetchCostReport({ period: 'weekly' }).catch(() => {})
  }, [fetchUsageStatistics, fetchCostReport])

  const global = usageStats?.global_stats
  const todayTokens = global?.daily_tokens ?? 0
  const todayCost = global?.estimated_daily_cost ?? 0
  const weekTokens = costReport?.total_tokens ?? global?.monthly_tokens ?? 0
  const weekCost = costReport?.total_cost ?? global?.estimated_monthly_cost ?? 0

  const dailyBars = useMemo(() => {
    const rows = costReport?.daily_breakdown ?? []
    return rows
      .map((row) => {
        const tokens =
          Number(row.tokens ?? row.total_tokens ?? row.token_count ?? 0) || 0
        const date = String(row.date ?? row.day ?? row.timestamp ?? '')
        return { date, tokens, label: dayLabel(date) }
      })
      .slice(-7)
  }, [costReport])

  const maxBar = Math.max(1, ...dailyBars.map((b) => b.tokens))

  const modelRows = useMemo(() => {
    const byModel = costReport?.by_model ?? {}
    const entries = Object.entries(byModel).map(([name, raw]) => {
      const tokens = Number(
        (raw as { tokens?: number; total_tokens?: number })?.tokens ??
          (raw as { total_tokens?: number })?.total_tokens ??
          0,
      )
      return { name, tokens }
    })
    // 若报表无模型数据，从 recent_records 聚合
    if (entries.length === 0 && usageStats?.recent_records?.length) {
      const map = new Map<string, number>()
      for (const r of usageStats.recent_records) {
        map.set(r.model || 'unknown', (map.get(r.model || 'unknown') || 0) + (r.tokens || 0))
      }
      return Array.from(map.entries())
        .map(([name, tokens]) => ({ name, tokens }))
        .sort((a, b) => b.tokens - a.tokens)
        .slice(0, 6)
    }
    return entries.sort((a, b) => b.tokens - a.tokens).slice(0, 6)
  }, [costReport, usageStats])

  const modelMax = Math.max(1, ...modelRows.map((m) => m.tokens))

  return (
    <div
      className="flex h-full flex-col gap-5 overflow-auto p-6"
      style={{ background: 'var(--ds-bg-panel, hsl(var(--card)))' }}
      data-testid="cost-dashboard"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-foreground text-[15px] font-semibold">成本看板</h2>
        {isLoading && (
          <span className="text-muted-foreground font-mono text-[10px]">同步中…</span>
        )}
      </div>

      {error && (
        <div className="border-status-error/40 bg-status-error/10 text-status-error rounded-lg border px-3 py-2 text-xs">
          {error}
        </div>
      )}

      {/* 统计卡行 */}
      <div className="grid grid-cols-2 gap-4">
        <StatCard
          label="今日消耗"
          value={formatCny(todayCost)}
          meta={`${formatTokens(todayTokens)} tok`}
          accent="var(--ds-accent-primary, #22D3EE)"
        />
        <StatCard
          label="本周消耗"
          value={formatCny(weekCost)}
          meta={`${formatTokens(weekTokens)} tok`}
          accent="var(--ds-accent-ai, #A78BFA)"
        />
      </div>

      {/* 近 7 日 */}
      <section
        className="rounded-[10px] border p-4"
        style={{
          background: 'var(--ds-bg-canvas, #04060F)',
          borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
        }}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-foreground text-[13px] font-medium">近 7 日 Token</h3>
          <span className="text-muted-foreground font-mono text-[10px]">
            max {formatTokens(maxBar)}
          </span>
        </div>
        {dailyBars.length === 0 ? (
          <EmptyHint text="暂无日报数据" />
        ) : (
          <div className="flex h-28 items-end gap-2">
            {dailyBars.map((bar) => {
              const h = Math.max(4, Math.round((bar.tokens / maxBar) * 100))
              return (
                <div key={bar.date || bar.label} className="flex flex-1 flex-col items-center gap-1">
                  <div
                    className="w-full rounded-t-sm"
                    style={{
                      height: `${h}%`,
                      minHeight: 4,
                      background:
                        'linear-gradient(180deg, var(--ds-accent-primary,#22D3EE) 0%, rgba(34,211,238,0.25) 100%)',
                    }}
                    title={`${bar.label}: ${formatTokens(bar.tokens)}`}
                  />
                  <span className="text-muted-foreground font-mono text-[9px]">{bar.label}</span>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* 模型用量 */}
      <section
        className="rounded-[10px] border p-4"
        style={{
          background: 'var(--ds-bg-canvas, #04060F)',
          borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
        }}
      >
        <h3 className="text-foreground mb-3 text-[13px] font-medium">模型用量</h3>
        {modelRows.length === 0 ? (
          <EmptyHint text="暂无模型拆分数据" />
        ) : (
          <div className="flex flex-col gap-3">
            {modelRows.map((row) => {
              const pct = Math.round((row.tokens / modelMax) * 100)
              return (
                <div key={row.name}>
                  <div className="mb-1 flex items-center justify-between">
                    <span className="text-secondary truncate text-[12px]">{row.name}</span>
                    <span className="text-muted-foreground font-mono text-[10px]">
                      {formatTokens(row.tokens)}
                    </span>
                  </div>
                  <div
                    className="h-1 overflow-hidden rounded-sm"
                    style={{ background: '#121C38' }}
                  >
                    <div
                      className="h-full rounded-sm transition-all"
                      style={{
                        width: `${pct}%`,
                        background: 'var(--ds-status-info, #60A5FA)',
                      }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}

function StatCard({
  label,
  value,
  meta,
  accent,
}: {
  label: string
  value: string
  meta: string
  accent: string
}) {
  return (
    <div
      className={cn('rounded-[10px] border px-4 py-3')}
      style={{
        background: 'var(--ds-bg-canvas, #04060F)',
        borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
      }}
    >
      <div className="text-muted-foreground mb-2 text-[11px]">{label}</div>
      <div className="text-[20px] font-semibold tracking-tight" style={{ color: accent }}>
        {value}
      </div>
      <div className="text-muted-foreground mt-1 font-mono text-[10px]">{meta}</div>
    </div>
  )
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="text-muted-foreground flex h-16 items-center justify-center text-xs">
      {text}
    </div>
  )
}
