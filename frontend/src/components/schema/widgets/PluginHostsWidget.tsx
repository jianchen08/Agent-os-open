/**
 * PluginHostsWidget — 监控页「插件」tab 的进程观测视图
 *
 * 数据源：内核只读端点 GET /api/v1/plugins/hosts（admin 鉴权），10s 轮询
 * 对齐内核采集周期；并 join 运行态端点 GET /ext/monitoring/plugins（30s 轮询，
 * 对齐原「插件运行」表周期）补每插件上次崩溃时间——该列 hosts 契约没有，
 * 两现成端点前端 join（用户裁定：运行表与卡片是同一数据，不并存，列全迁卡片）。
 *
 * 汇总条内存为全口径分段：插件段（hosts 求和）+ 内核段（GET
 * /api/v1/system/memstats 的内核进程 RSS）+ 应用壳段（Electron IPC
 * app:metrics 的进程树 workingSet，Web 形态缺失显 —）；全应用合计仅三段
 * 全有值时给出（任一缺失不虚构合计）。
 *
 * 渲染：按进程分组卡片，插件名是主角——独占进程直接以插件名为题，共享进程
 * 以「N 个插件共享进程」为题、成员插件按行呈现状态词（运行中/启动中/等待
 * 重新加载/已停止）与上次崩溃；PID/内存/运行时长为次要信息。卡片级红色警示：
 * 进程未响应已停止 / 调用长时间未返回疑似卡住（最后调用距今 >10 分钟）/
 * 启动迟迟未完成。已停止进程卡片与「未装载」（已启用但当前没有运行进程的
 * 插件）区收进底部折叠区（用户裁定：非运行面不占主视野，默认收起可展开），
 * 顶部汇总条保留全量计数。运行态端点失败不阻塞主视图：卡片照常渲染，
 * 底部给一行降级提示。
 */

import { useMemo, useState } from 'react'
import { AlertTriangle, ChevronDown } from '@/assets/icons'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { useAppMetricsQuery } from '@/hooks/queries/useAppMetricsQuery'
import { useKernelMemStatsQuery } from '@/hooks/queries/useKernelMemStatsQuery'
import { usePluginHostsQuery } from '@/hooks/queries/usePluginHostsQuery'
import { usePluginRuntimeQuery } from '@/hooks/queries/usePluginRuntimeQuery'
import { formatDate } from '@/utils/format'
import type { HostMember, PendingSpawnEntry, PluginHost } from '@/types/pluginHosts'

/** 「调用长时间未返回」判定窗（ms）：进行中的调用>0 且最后调用距今超过该窗视为卡住嫌疑 */
const STALE_IN_FLIGHT_MS = 10 * 60 * 1000

/** epoch 秒 → 相对时间（从未调用/非法值 → '—'） */
function formatEpochRelative(secs: number | null | undefined): string {
  if (typeof secs !== 'number' || !Number.isFinite(secs)) return '—'
  return formatDate(new Date(secs * 1000).toISOString(), 'relative')
}

/** 运行时长（秒）→ 人读时长（非法/负值 → '—'） */
function formatUptime(secs: number | null | undefined): string {
  if (typeof secs !== 'number' || !Number.isFinite(secs) || secs < 0) return '—'
  const days = Math.floor(secs / 86400)
  const hours = Math.floor((secs % 86400) / 3600)
  const minutes = Math.floor((secs % 3600) / 60)
  if (days > 0) return `${days}天${hours}小时`
  if (hours > 0) return `${hours}小时${minutes}分`
  if (minutes > 0) return `${minutes}分`
  return `${Math.floor(secs)}秒`
}

/** 内存（MB）→ 人读值（缺失 → '—'） */
function formatRss(rssMb: number | null | undefined): string {
  if (typeof rssMb !== 'number' || !Number.isFinite(rssMb)) return '—'
  return `${rssMb.toFixed(1)} MB`
}

/** 运行态行清单 → 插件 id → 上次崩溃 epoch 秒（仅收 0/非法值之外的崩溃记录） */
function buildCrashIndex(rows: Array<{ plugin_id: string; last_crash_ts?: number | null }>): Map<string, number> {
  const map = new Map<string, number>()
  for (const row of rows) {
    if (typeof row.last_crash_ts === 'number' && row.last_crash_ts > 0) {
      map.set(row.plugin_id, row.last_crash_ts)
    }
  }
  return map
}

/** 徽标通用样式（圆角胶囊小字） */
const BADGE_CLS = 'inline-flex items-center rounded-full px-2 py-0.5 text-xs'

/** 卡片标题：插件名是主角——独占进程（单成员）直接用插件名，共享进程
 * （多成员）报「N 个插件共享进程」；无成员快照时退回进程标识 */
function hostTitle(host: PluginHost): string {
  const names = (host.members ?? []).map((m) => m.plugin_id).filter(Boolean)
  if (names.length >= 2) return `${names.length} 个插件共享进程`
  if (names.length === 1) return names[0]
  return host.host_key
}

/** 进程运行徽标：true=运行中；false 走卡片红色警示（进程未响应，已停止），不重复标 */
function AliveBadge({ alive }: { alive: boolean | null | undefined }) {
  if (alive !== true) return null
  return <span className={`${BADGE_CLS} bg-status-success/10 text-status-success`}>运行中</span>
}

/** 成员插件状态词（迁移自原「插件运行」表状态列，hosts 数据派生）：
 * 「启动中」蓝标优先（启动窗口是瞬时态），其次「等待重新加载」黄标；
 * 进程实际成员快照存在时按含否给「运行中」/「已停止」，快照缺失不猜测。 */
function memberBadge(
  member: HostMember,
  host: PluginHost,
): { label?: string; cls: string } {
  if (host.starting_members?.includes(member.plugin_id)) {
    return { label: '启动中', cls: 'bg-status-info/10 text-status-info' }
  }
  if (member.pending_rejoin === true) {
    return { label: '等待重新加载', cls: 'bg-status-warning/10 text-status-warning' }
  }
  const spawned = host.spawned_members
  if (spawned == null) {
    return { cls: 'bg-accent/30 text-muted-foreground' }
  }
  if (spawned.includes(member.plugin_id)) {
    return { label: '运行中', cls: 'bg-accent/30 text-muted-foreground' }
  }
  return { label: '已停止', cls: 'bg-status-error/10 text-status-error' }
}

/** 卡片级红色警示（空数组 = 无警示） */
function hostAlerts(host: PluginHost, nowMs: number): string[] {
  const alerts: string[] = []
  if (host.alive === false) {
    alerts.push('进程未响应，已停止')
  }
  const inFlight = host.in_flight ?? 0
  const lastCallMs = typeof host.last_call_at === 'number' ? host.last_call_at * 1000 : null
  if (inFlight > 0 && lastCallMs !== null && nowMs - lastCallMs > STALE_IN_FLIGHT_MS) {
    alerts.push(`调用长时间未返回，疑似卡住（最后调用 ${formatEpochRelative(host.last_call_at)}）`)
  }
  if (host.starting === true) {
    alerts.push('进程启动中迟迟未完成')
  }
  return alerts
}

/** 单个进程卡片（一个进程内住一个或多个插件） */
function HostCard({
  host,
  nowMs,
  crashByPlugin,
}: {
  host: PluginHost
  nowMs: number
  crashByPlugin: Map<string, number>
}) {
  const alerts = hostAlerts(host, nowMs)
  const members = host.members ?? []
  const title = hostTitle(host)
  return (
    <div
      data-testid="host-card"
      className={`space-y-2 rounded-lg border p-3 ${
        alerts.length > 0 ? 'border-status-error/60 bg-status-error/5' : 'border-border'
      }`}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span
          data-testid="host-card-title"
          className="min-w-0 truncate text-sm font-medium"
          title={title}
        >
          {title}
        </span>
        <AliveBadge alive={host.alive} />
        {alerts.length > 0 && <AlertTriangle className="text-status-error h-4 w-4 shrink-0" />}
      </div>

      <div className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-0.5 text-xs">
        <span>PID {host.pid ?? '—'}</span>
        <span>内存 {formatRss(host.rss_mb)}</span>
        <span>运行 {formatUptime(host.uptime_secs)}</span>
        <span>进行中的调用 {host.in_flight ?? 0}</span>
        <span>最后调用 {formatEpochRelative(host.last_call_at)}</span>
      </div>

      {alerts.length > 0 && (
        <ul data-testid="host-alerts" className="text-status-error space-y-0.5 text-xs">
          {alerts.map((alert) => (
            <li key={alert}>{alert}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-1.5" data-testid="host-members">
        {members.map((member) => {
          const badge = memberBadge(member, host)
          const crash = crashByPlugin.get(member.plugin_id)
          return (
            <span key={member.plugin_id} className={`${BADGE_CLS} ${badge.cls}`}>
              {member.plugin_id}
              {badge.label && <span className="ml-1">{badge.label}</span>}
              {crash !== undefined && (
                <span className="ml-1">上次崩溃 {formatEpochRelative(crash)}</span>
              )}
            </span>
          )
        })}
      </div>
    </div>
  )
}

/** 「未装载」独立区：已启用但当前没有运行进程的插件逐项红显 */
function PendingSpawnSection({ entries }: { entries: PendingSpawnEntry[] }) {
  return (
    <section
      data-testid="pending-spawn"
      className="border-status-error/40 bg-status-error/5 space-y-1.5 rounded-lg border p-3"
    >
      <div className="text-status-error text-sm font-medium">未装载</div>
      <div className="text-status-error/80 text-xs">已启用但当前没有运行的进程</div>
      <ul className="space-y-1">
        {entries.map((entry) => (
          <li key={entry.plugin_id} className="text-status-error flex flex-wrap gap-x-3 text-xs">
            <span className="font-medium">{entry.plugin_id}</span>
            <span>最后调用 {formatEpochRelative(entry.last_call_at)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** 全口径内存分段换算：内核段（memstats 字节→MB）/ 应用壳段（workingSet KB→MB），
 * 非法值归 null（汇总条该段显 —，不虚构 0） */
function bytesToMb(bytes: number | null | undefined): number | null {
  return typeof bytes === 'number' && Number.isFinite(bytes) ? bytes / (1024 * 1024) : null
}
function kbToMb(kb: number | null | undefined): number | null {
  return typeof kb === 'number' && Number.isFinite(kb) ? kb / 1024 : null
}

/** 汇总条：进程/状态计数 + 全口径内存（插件段=进程观测卡片求和；内核段=
 * memstats 内核进程 RSS；应用壳段=Electron 进程树 workingSet；全应用=三段
 * 合计，任一段缺失不虚构合计显 —）。rss 缺失（null）不进求和；全部缺失
 * 显 —。 */
function HostsSummary({
  hosts,
  pendingCount,
  kernelRssMb,
  shellRssMb,
}: {
  hosts: PluginHost[]
  pendingCount: number
  kernelRssMb: number | null
  shellRssMb: number | null
}) {
  const aliveCount = hosts.filter((h) => h.alive === true).length
  const deadCount = hosts.filter((h) => h.alive === false).length
  const totalRss = hosts.reduce(
    (sum, h) => (typeof h.rss_mb === 'number' ? sum + h.rss_mb : sum),
    0,
  )
  const anyRss = hosts.some((h) => typeof h.rss_mb === 'number')
  const fullTotal =
    anyRss && kernelRssMb !== null && shellRssMb !== null
      ? totalRss + kernelRssMb + shellRssMb
      : null
  const memberCount = hosts.reduce((sum, h) => sum + (h.members?.length ?? 0), 0)
  const formatMb = (mb: number | null) => (mb !== null ? `${mb.toFixed(1)} MB` : '—')
  return (
    <div
      data-testid="hosts-summary"
      className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 rounded-lg border p-2.5 text-xs"
    >
      <span>进程 {hosts.length}</span>
      <span>运行中 {aliveCount}</span>
      <span>已停止 {deadCount}</span>
      <span className="text-foreground font-medium">插件内存 {formatMb(anyRss ? totalRss : null)}</span>
      <span>内核 {formatMb(kernelRssMb)}</span>
      <span>应用壳 {formatMb(shellRssMb)}</span>
      <span className="text-foreground font-medium">全应用 {formatMb(fullTotal)}</span>
      <span>承载插件 {memberCount}</span>
      {pendingCount > 0 && <span>未装载 {pendingCount}</span>}
    </div>
  )
}

/** 底部折叠区：已停止进程卡片 + 未装载插件——非运行面默认收起不占主视野，
 * 头部报计数（点击展开），两者皆空不渲染。汇总条仍保留全量计数。 */
function StoppedPendingSection({
  stoppedHosts,
  pendingSpawn,
  crashByPlugin,
  nowMs,
}: {
  stoppedHosts: PluginHost[]
  pendingSpawn: PendingSpawnEntry[]
  crashByPlugin: Map<string, number>
  nowMs: number
}) {
  const [open, setOpen] = useState(false)
  const label = [
    stoppedHosts.length > 0 && `已停止 ${stoppedHosts.length}`,
    pendingSpawn.length > 0 && `未装载 ${pendingSpawn.length}`,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <section data-testid="stopped-pending" className="space-y-2">
      <button
        type="button"
        data-testid="stopped-pending-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs"
      >
        <ChevronDown
          className={`h-3.5 w-3.5 transition-transform ${open ? '' : '-rotate-90'}`}
        />
        {label}
      </button>
      {open && (
        <div className="space-y-3">
          {stoppedHosts.length > 0 && (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {stoppedHosts.map((host) => (
                <HostCard
                  key={host.host_key}
                  host={host}
                  nowMs={nowMs}
                  crashByPlugin={crashByPlugin}
                />
              ))}
            </div>
          )}
          {pendingSpawn.length > 0 && <PendingSpawnSection entries={pendingSpawn} />}
        </div>
      )}
    </section>
  )
}

/** 插件进程观测视图 widget（监控页「插件」组台声明承载） */
export function PluginHostsWidget() {
  const hostsQuery = usePluginHostsQuery()
  const runtimeQuery = usePluginRuntimeQuery()
  const memStatsQuery = useKernelMemStatsQuery()
  const appMetricsQuery = useAppMetricsQuery()
  const hosts = hostsQuery.data?.hosts ?? []
  const pendingSpawn = hostsQuery.data?.pending_spawn ?? []

  const crashByPlugin = useMemo(
    () => buildCrashIndex(runtimeQuery.data ?? []),
    [runtimeQuery.data],
  )

  if (hostsQuery.isPending && !hostsQuery.data) {
    return (
      <div data-testid="plugin-hosts-widget">
        <LoadingState variant="skeleton" skeletonCount={3} />
      </div>
    )
  }

  if (hostsQuery.isError) {
    return (
      <div data-testid="plugin-hosts-widget">
        <ErrorState
          message={hostsQuery.error instanceof Error ? hostsQuery.error.message : '获取插件运行状态失败'}
          onRetry={() => void hostsQuery.refetch()}
        />
      </div>
    )
  }

  if (hosts.length === 0 && pendingSpawn.length === 0) {
    return (
      <div data-testid="plugin-hosts-widget" className="text-muted-foreground py-12 text-center">
        暂无插件运行数据
      </div>
    )
  }

  const nowMs = Date.now()
  // alive=false 才算已停止（快照缺失 null 不猜测，留在主视野）
  const runningHosts = hosts.filter((h) => h.alive !== false)
  const stoppedHosts = hosts.filter((h) => h.alive === false)
  return (
    <div data-testid="plugin-hosts-widget" className="space-y-3">
      <HostsSummary
        hosts={hosts}
        pendingCount={pendingSpawn.length}
        kernelRssMb={bytesToMb(memStatsQuery.data?.process_rss_bytes)}
        shellRssMb={kbToMb(appMetricsQuery.data?.totalWorkingSetKb)}
      />
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {runningHosts.map((host) => (
          <HostCard key={host.host_key} host={host} nowMs={nowMs} crashByPlugin={crashByPlugin} />
        ))}
      </div>
      {(stoppedHosts.length > 0 || pendingSpawn.length > 0) && (
        <StoppedPendingSection
          stoppedHosts={stoppedHosts}
          pendingSpawn={pendingSpawn}
          crashByPlugin={crashByPlugin}
          nowMs={nowMs}
        />
      )}
      {runtimeQuery.isError && (
        <div data-testid="runtime-degraded" className="text-muted-foreground text-xs">
          崩溃记录暂不可用
        </div>
      )}
    </div>
  )
}
