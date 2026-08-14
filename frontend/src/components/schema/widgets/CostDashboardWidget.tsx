/**
 * 成本看板 Widget · Deep Space v2
 *
 * 设计来源：画布 C · WorkspacePanel · 看板内容 (49:277)
 * - 统计卡行：今日 / 本周消耗
 * - 近 7 日 Token 柱状
 * - 模型用量占比条
 * - 缓存命中卡（task_observability 1b）：本轮命中率 / 命中 / 未命中 /
 *   累计浪费 + 会话级命中率趋势（来自 track 插件 cost_update 实时推送，
 *   不依赖 HTTP）
 *
 * 数据：对接 /api/v1/cost-control/*（useCostControl）+ WS cost_update
 */

import { useEffect, useMemo } from 'react'
import { useCostControl } from '@/hooks/useCostControl'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useTerminationStore } from '@/stores/terminationStore'
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

  // ── 缓存命中维度（task_observability 1b）：来自 WS cost_update 实时推送 ──
  // 当前关注管道优先；无激活管道时取最近有数据的（按最后一条趋势时间戳）
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const usageByPipeline = useContextUsageStore((s) => s.usageByPipeline)
  const cacheUsage = useMemo(() => {
    if (activePipelineId && usageByPipeline[activePipelineId]?.hitRatio > 0) {
      return usageByPipeline[activePipelineId]
    }
    const entries = Object.values(usageByPipeline)
      .filter((u) => (u.cacheHistory?.length ?? 0) > 0)
      .sort(
        (a, b) =>
          (b.cacheHistory?.[b.cacheHistory.length - 1]?.ts ?? 0) -
          (a.cacheHistory?.[a.cacheHistory.length - 1]?.ts ?? 0),
      )
    return entries[0]
  }, [activePipelineId, usageByPipeline])

  const cacheTrend = useMemo(() => (cacheUsage?.cacheHistory ?? []).slice(-20), [cacheUsage])

  // ── 终止评估（task_observability 1c）：剩余预算 + 收敛信号指示器 ──
  const terminationByPipeline = useTerminationStore((s) => s.statusByPipeline)
  const termination = useMemo(() => {
    if (activePipelineId && terminationByPipeline[activePipelineId]) {
      return terminationByPipeline[activePipelineId]
    }
    const entries = Object.values(terminationByPipeline).sort((a, b) => b.ts - a.ts)
    return entries[0]
  }, [activePipelineId, terminationByPipeline])

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
        className="rounded-lg border p-4"
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
        className="rounded-lg border p-4"
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
                    style={{ background: 'var(--ds-bg-elevated, #121C38)' }}
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

      {/* 缓存命中（task_observability 1b）：实时推送，定位破坏 cache 前缀的轮次 */}
      <section
        data-testid="cost-cache-card"
        className="rounded-lg border p-4"
        style={{
          background: 'var(--ds-bg-canvas, #04060F)',
          borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
        }}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-foreground text-[13px] font-medium">缓存命中</h3>
          {cacheUsage && (
            <span className="text-muted-foreground font-mono text-[10px]">
              累计浪费 {formatTokens(cacheUsage.cumulative?.missed ?? 0)} tok
            </span>
          )}
        </div>
        {!cacheUsage ? (
          <EmptyHint text="暂无缓存数据（等待 LLM 调用）" />
        ) : (
          <>
            <div className="mb-3 grid grid-cols-3 gap-3">
              <div>
                <div className="text-muted-foreground mb-1 text-[10px]">本轮命中率</div>
                <div
                  className="text-[18px] font-semibold tracking-tight"
                  style={{
                    color:
                      cacheUsage.hitRatio >= 0.7
                        ? 'var(--ds-status-success, #34D399)'
                        : 'var(--ds-status-warning, #FBBF24)',
                  }}
                >
                  {(cacheUsage.hitRatio * 100).toFixed(1)}%
                </div>
              </div>
              <div>
                <div className="text-muted-foreground mb-1 text-[10px]">本轮命中</div>
                <div className="text-secondary font-mono text-[14px]">
                  {formatTokens(cacheUsage.cachedTokens)}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground mb-1 text-[10px]">本轮未命中</div>
                <div className="text-secondary font-mono text-[14px]">
                  {formatTokens(cacheUsage.missedTokens)}
                </div>
              </div>
            </div>
            {/* 会话级命中率趋势：骤降点定位哪轮破坏了 cache 前缀 */}
            <div>
              <div className="text-muted-foreground mb-1 text-[10px]">会话命中率趋势（近 20 轮）</div>
              <div data-testid="cost-cache-trend" className="flex h-10 items-end gap-[3px]">
                {cacheTrend.map((point, i) => (
                  <div
                    key={`${point.ts}-${i}`}
                    data-testid="cache-trend-bar"
                    className="flex-1 rounded-t-sm"
                    style={{
                      height: `${Math.max(6, Math.round(point.hitRatio * 100))}%`,
                      background:
                        point.hitRatio >= 0.7
                          ? 'var(--ds-status-success, #34D399)'
                          : 'var(--ds-status-warning, #FBBF24)',
                      opacity: 0.35 + point.hitRatio * 0.65,
                    }}
                    title={`第 ${i + 1} 轮：命中 ${(point.hitRatio * 100).toFixed(1)}%（未命中 ${point.missedTokens} tok）`}
                  />
                ))}
              </div>
            </div>
          </>
        )}
      </section>

      {/* 运行状态（task_observability 1c）：剩余预算 + 收敛信号 */}
      <section
        data-testid="termination-indicator"
        className="rounded-lg border p-4"
        style={{
          background: 'var(--ds-bg-canvas, #04060F)',
          borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
        }}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-foreground text-[13px] font-medium">运行状态</h3>
          {termination && (
            <span className="text-muted-foreground font-mono text-[10px]">
              第 {termination.iteration} 轮 · {Math.round(termination.elapsedS)}s
            </span>
          )}
        </div>
        {!termination ? (
          <EmptyHint text="暂无运行数据（等待管道启动）" />
        ) : (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="text-muted-foreground mb-1 text-[10px]">剩余预算</div>
              {termination.remainingBudgetPercent === null ? (
                <div className="text-muted-foreground text-[14px]">未启用</div>
              ) : (
                <>
                  <div
                    className="text-[18px] font-semibold tracking-tight"
                    style={{
                      color:
                        termination.remainingBudgetPercent >= 30
                          ? 'var(--ds-status-success, #34D399)'
                          : 'var(--ds-status-warning, #FBBF24)',
                    }}
                  >
                    {termination.remainingBudgetPercent.toFixed(1)}%
                  </div>
                  <div
                    className="mt-1 h-1 overflow-hidden rounded-sm"
                    style={{ background: 'var(--ds-bg-elevated, #121C38)' }}
                  >
                    <div
                      className="h-full rounded-sm transition-all"
                      style={{
                        width: `${Math.min(100, Math.max(0, termination.remainingBudgetPercent))}%`,
                        background: 'var(--ds-accent-primary, #22D3EE)',
                      }}
                    />
                  </div>
                </>
              )}
            </div>
            <div>
              <div className="text-muted-foreground mb-1 text-[10px]">收敛信号</div>
              <div
                className="text-[14px] font-medium"
                style={{
                  color:
                    termination.convergence === 'converging'
                      ? 'var(--ds-status-success, #34D399)'
                      : 'var(--ds-status-warning, #FBBF24)',
                }}
              >
                {termination.convergence === 'stalled'
                  ? '停滞'
                  : termination.convergence === 'budget_critical'
                    ? '预算临界'
                    : '收敛中'}
              </div>
              {termination.shouldStop && termination.stopReason && (
                <div className="text-muted-foreground mt-1 text-[10px] break-all">
                  终止：{termination.stopReason}
                </div>
              )}
            </div>
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
      className={cn('rounded-lg border px-4 py-3')}
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
