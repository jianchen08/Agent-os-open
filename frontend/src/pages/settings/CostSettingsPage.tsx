/** 费用控制配置页面 Token 用量限制和预算管理：预算限制、Token 用量追踪、费用告警、使用统计 */

import { Loader2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  getCostControlConfig,
  saveCostControlConfig,
  type CostControlConfigResponse,
  type CostControlGlobalConfig,
  type CostControlAlertsConfig,
  type CostControlProtectionConfig,
} from '@/services/api/config'
import {
  getBudgetStatus,
  getUsageStatistics,
  type BudgetStatusResponse,
  type UsageStatisticsResponse,
} from '@/services/api/costControl'

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

const DEFAULT_CONFIG: CostControlConfigResponse = {
  global_config: {
    daily_token_limit: 1000000,
    monthly_token_limit: 30000000,
    per_task_token_limit: 200000,
    per_session_token_limit: 500000,
  },
  alerts: {
    warning_threshold: 70,
    critical_threshold: 90,
    exhausted_threshold: 100,
  },
  protection: {
    auto_save_at_warning: true,
    auto_pause_at_critical: true,
    auto_stop_at_exhausted: true,
  },
  enabled: true,
}

/** 费用控制配置页面组件 */
export function CostSettingsPage() {
  const [config, setConfig] = useState<CostControlConfigResponse>(DEFAULT_CONFIG)
  const [budget, setBudget] = useState<BudgetStatusResponse | null>(null)
  const [stats, setStats] = useState<UsageStatisticsResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [activeTab, setActiveTab] = useState('overview')

  // 加载配置和数据
  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)

    Promise.all([
      getCostControlConfig(),
      getBudgetStatus(),
      getUsageStatistics(),
    ])
      .then(([configData, budgetData, statsData]) => {
        if (cancelled) return
        setConfig(configData)
        setBudget(budgetData)
        setStats(statsData)
      })
      .catch(() => {
        if (!cancelled) {
          setLoadError('无法连接服务器，请稍后重试')
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 更新全局配置
  const updateGlobal = useCallback(
    <K extends keyof CostControlGlobalConfig>(key: K, value: number) => {
      setConfig((prev) => ({
        ...prev,
        global_config: { ...prev.global_config, [key]: value },
      }))
    },
    [],
  )

  // 更新告警配置
  const updateAlert = useCallback(
    <K extends keyof CostControlAlertsConfig>(key: K, value: number) => {
      setConfig((prev) => ({
        ...prev,
        alerts: { ...prev.alerts, [key]: value },
      }))
    },
    [],
  )

  // 更新保护配置
  const updateProtection = useCallback(
    <K extends keyof CostControlProtectionConfig>(key: K, value: boolean) => {
      setConfig((prev) => ({
        ...prev,
        protection: { ...prev.protection, [key]: value },
      }))
    },
    [],
  )

  // 保存
  const handleSave = useCallback(async () => {
    setSaveState('saving')
    try {
      const saved = await saveCostControlConfig(config)
      setConfig(saved)
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch {
      setSaveState('error')
    }
  }, [config])

  // 切换启用状态
  const toggleEnabled = useCallback(() => {
    setConfig((prev) => ({ ...prev, enabled: !prev.enabled }))
  }, [])

  // 格式化数字
  const fmt = (n: number) => n.toLocaleString()
  const fmtK = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n))

  if (isLoading) {
    return (
      <PageShell title="成本控制" description="Token 用量限制和预算管理">
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载中...
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell title="成本控制" description="Token 用量限制和预算管理">
      {loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="overview">概览</TabsTrigger>
          <TabsTrigger value="limits">预算限制</TabsTrigger>
          <TabsTrigger value="alerts">告警设置</TabsTrigger>
          <TabsTrigger value="usage">使用统计</TabsTrigger>
        </TabsList>

        {/* 概览 */}
        <TabsContent value="overview">
          <div className="mt-4 space-y-4">
            {/* 启用开关 */}
            <div className="bg-card flex items-center justify-between rounded-lg border px-3 py-2">
              <div>
                <span className="text-sm font-medium">成本控制</span>
                <span className="text-muted-foreground ml-2 text-xs">
                  {config.enabled ? '已启用' : '已禁用'}
                </span>
              </div>
              <button
                onClick={toggleEnabled}
                className={`relative h-5 w-10 rounded-full transition-colors ${
                  config.enabled ? 'bg-green-500' : 'bg-gray-400'
                }`}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${
                    config.enabled ? 'left-5' : 'left-0.5'
                  }`}
                />
              </button>
            </div>

            {/* 预算概览卡片 */}
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-card rounded-lg border px-4 py-3">
                <div className="text-muted-foreground mb-1 text-xs">今日用量</div>
                <div className="text-lg font-semibold">
                  {stats ? fmtK(stats.global_stats.daily_tokens) : '--'}
                </div>
                <Progress
                  value={stats?.global_stats.daily_usage_percent ?? 0}
                  variant={
                    stats && stats.global_stats.daily_usage_percent > 80 ? 'error' : 'default'
                  }
                  className="mt-2 h-1.5"
                />
                <div className="text-muted-foreground mt-1 text-xs">
                  限制: {fmt(config.global_config.daily_token_limit)}
                </div>
              </div>
              <div className="bg-card rounded-lg border px-4 py-3">
                <div className="text-muted-foreground mb-1 text-xs">本月用量</div>
                <div className="text-lg font-semibold">
                  {stats ? fmtK(stats.global_stats.monthly_tokens) : '--'}
                </div>
                <Progress
                  value={stats?.global_stats.monthly_usage_percent ?? 0}
                  variant={
                    stats && stats.global_stats.monthly_usage_percent > 80 ? 'warning' : 'default'
                  }
                  className="mt-2 h-1.5"
                />
                <div className="text-muted-foreground mt-1 text-xs">
                  限制: {fmt(config.global_config.monthly_token_limit)}
                </div>
              </div>
            </div>

            {/* 费用概览 */}
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-card rounded-lg border px-4 py-3">
                <div className="text-muted-foreground mb-1 text-xs">今日估算成本</div>
                <div className="text-foreground text-lg font-semibold">
                  ${stats?.global_stats.estimated_daily_cost.toFixed(2) ?? '--'}
                </div>
              </div>
              <div className="bg-card rounded-lg border px-4 py-3">
                <div className="text-muted-foreground mb-1 text-xs">本月估算成本</div>
                <div className="text-foreground text-lg font-semibold">
                  ${stats?.global_stats.estimated_monthly_cost.toFixed(2) ?? '--'}
                </div>
              </div>
            </div>

            {/* 保护状态 */}
            <div className="bg-card rounded-lg border px-4 py-3">
              <div className="text-muted-foreground mb-2 text-xs">保护策略</div>
              <div className="flex flex-wrap gap-2">
                <ProtectionBadge
                  label="警告时自动保存"
                  enabled={config.protection.auto_save_at_warning}
                  threshold={`${config.alerts.warning_threshold}%`}
                />
                <ProtectionBadge
                  label="严重时自动暂停"
                  enabled={config.protection.auto_pause_at_critical}
                  threshold={`${config.alerts.critical_threshold}%`}
                />
                <ProtectionBadge
                  label="耗尽时自动停止"
                  enabled={config.protection.auto_stop_at_exhausted}
                  threshold={`${config.alerts.exhausted_threshold}%`}
                />
              </div>
            </div>
          </div>
        </TabsContent>

        {/* 预算限制 */}
        <TabsContent value="limits">
          <div className="mt-4 space-y-4">
            <FieldRow label="每日 Token 限制" htmlFor="cost-daily">
              <Input
                id="cost-daily"
                type="number"
                min={1000}
                value={config.global_config.daily_token_limit}
                onChange={(e) => updateGlobal('daily_token_limit', Number(e.target.value))}
              />
              <span className="text-muted-foreground mt-1 text-xs">
                当前: {fmtK(config.global_config.daily_token_limit)} tokens
              </span>
            </FieldRow>
            <FieldRow label="每月 Token 限制" htmlFor="cost-monthly">
              <Input
                id="cost-monthly"
                type="number"
                min={10000}
                value={config.global_config.monthly_token_limit}
                onChange={(e) => updateGlobal('monthly_token_limit', Number(e.target.value))}
              />
              <span className="text-muted-foreground mt-1 text-xs">
                当前: {fmtK(config.global_config.monthly_token_limit)} tokens
              </span>
            </FieldRow>
            <FieldRow label="单任务 Token 限制" htmlFor="cost-task">
              <Input
                id="cost-task"
                type="number"
                min={1000}
                value={config.global_config.per_task_token_limit}
                onChange={(e) => updateGlobal('per_task_token_limit', Number(e.target.value))}
              />
            </FieldRow>
            <FieldRow label="单会话 Token 限制" htmlFor="cost-session">
              <Input
                id="cost-session"
                type="number"
                min={1000}
                value={config.global_config.per_session_token_limit}
                onChange={(e) => updateGlobal('per_session_token_limit', Number(e.target.value))}
              />
            </FieldRow>
          </div>
        </TabsContent>

        {/* 告警设置 */}
        <TabsContent value="alerts">
          <div className="mt-4 space-y-4">
            <FieldRow label="警告阈值 (%)" htmlFor="alert-warning">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="alert-warning"
                  min={30}
                  max={100}
                  value={config.alerts.warning_threshold}
                  onChange={(e) => updateAlert('warning_threshold', Number(e.target.value))}
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="min-w-[40px] text-right text-sm">
                  {config.alerts.warning_threshold}%
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={config.protection.auto_save_at_warning}
                  onChange={(e) => updateProtection('auto_save_at_warning', e.target.checked)}
                  className="rounded"
                />
                <span className="text-muted-foreground text-xs">自动保存当前进度</span>
              </div>
            </FieldRow>
            <FieldRow label="严重阈值 (%)" htmlFor="alert-critical">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="alert-critical"
                  min={50}
                  max={100}
                  value={config.alerts.critical_threshold}
                  onChange={(e) => updateAlert('critical_threshold', Number(e.target.value))}
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="min-w-[40px] text-right text-sm">
                  {config.alerts.critical_threshold}%
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={config.protection.auto_pause_at_critical}
                  onChange={(e) => updateProtection('auto_pause_at_critical', e.target.checked)}
                  className="rounded"
                />
                <span className="text-muted-foreground text-xs">自动暂停新任务</span>
              </div>
            </FieldRow>
            <FieldRow label="耗尽阈值 (%)" htmlFor="alert-exhausted">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="alert-exhausted"
                  min={80}
                  max={100}
                  value={config.alerts.exhausted_threshold}
                  onChange={(e) => updateAlert('exhausted_threshold', Number(e.target.value))}
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="min-w-[40px] text-right text-sm">
                  {config.alerts.exhausted_threshold}%
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={config.protection.auto_stop_at_exhausted}
                  onChange={(e) => updateProtection('auto_stop_at_exhausted', e.target.checked)}
                  className="rounded"
                />
                <span className="text-muted-foreground text-xs">自动停止所有任务</span>
              </div>
            </FieldRow>
          </div>
        </TabsContent>

        {/* 使用统计 */}
        <TabsContent value="usage">
          <div className="mt-4 space-y-4">
            {/* 任务级统计 */}
            {stats && stats.tasks.length > 0 && (
              <div>
                <h3 className="mb-2 text-sm font-semibold">任务使用统计</h3>
                <div className="space-y-2">
                  {stats.tasks.slice(0, 5).map((task) => (
                    <div
                      key={task.task_id}
                      className="bg-card flex items-center gap-3 rounded-lg border px-3 py-2"
                    >
                      <span className="text-muted-foreground min-w-[80px] truncate font-mono text-xs">
                        {task.task_id.slice(0, 8)}...
                      </span>
                      <div className="flex-1">
                        <Progress
                          value={task.usage_percent}
                          variant={
                            task.usage_percent > 80
                              ? 'error'
                              : task.usage_percent > 50
                                ? 'warning'
                                : 'default'
                          }
                          className="h-1.5"
                        />
                      </div>
                      <span className="min-w-[80px] text-right text-xs">
                        {fmtK(task.tokens)} / {fmtK(task.limit)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 最近使用记录 */}
            <div>
              <h3 className="mb-2 text-sm font-semibold">最近使用记录</h3>
              <div className="overflow-hidden rounded-lg border">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-card border-b">
                      <th className="px-3 py-2 text-left font-medium">时间</th>
                      <th className="px-3 py-2 text-left font-medium">模型</th>
                      <th className="px-3 py-2 text-right font-medium">Tokens</th>
                      <th className="px-3 py-2 text-right font-medium">成本</th>
                    </tr>
                  </thead>
                  <tbody>
                    {/* 加载失败或无记录时显示空状态提示 */}
                    {stats?.recent_records?.length ? (
                      stats.recent_records.map((record, idx) => (
                        <tr key={idx} className="border-b last:border-b-0">
                          <td className="text-muted-foreground px-3 py-2">
                            {new Date(record.timestamp).toLocaleString('zh-CN', {
                              month: '2-digit',
                              day: '2-digit',
                              hour: '2-digit',
                              minute: '2-digit',
                            })}
                          </td>
                          <td className="px-3 py-2">{record.model}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmtK(record.tokens)}</td>
                          <td className="px-3 py-2 text-right font-mono">
                            ${record.cost.toFixed(4)}
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={4} className="text-muted-foreground px-3 py-6 text-center">
                          暂无使用记录
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </TabsContent>
      </Tabs>

      {/* 保存按钮 */}
      <div className="mt-6 flex items-center gap-3 border-t pt-4">
        <Button onClick={handleSave} disabled={saveState === 'saving'}>
          {saveState === 'saving' ? (
            <>
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              保存中...
            </>
          ) : (
            '保存配置'
          )}
        </Button>
        {saveState === 'saved' && <span className="text-xs text-status-success" role="status">已保存</span>}
        {saveState === 'error' && <span className="text-xs text-status-error" role="alert">保存失败</span>}
      </div>
    </PageShell>
  )
}

/** 保护策略标签 */
function ProtectionBadge({
  label,
  enabled,
  threshold,
}: {
  label: string
  enabled: boolean
  threshold: string
}) {
  return (
    <span
      className={`rounded-full px-2 py-1 text-xs ${
        enabled ? 'bg-status-success/10 text-status-success' : 'bg-muted-foreground/10 text-muted-foreground'
      }`}
    >
      {label} ({threshold}) {enabled ? 'ON' : 'OFF'}
    </span>
  )
}

/* 共享子组件 */

function PageShell({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 设置
        </a>
        <h1 className="ml-4 text-base font-semibold">{title}</h1>
        <span className="text-muted-foreground ml-2 text-xs">{description}</span>
      </header>
      <main className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label="成本控制配置表单">{children}</main>
    </div>
  )
}

function FieldRow({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-4">
      <label
        htmlFor={htmlFor}
        className="text-muted-foreground text-sm sm:min-w-[120px] sm:shrink-0 sm:pt-2 sm:text-right"
      >
        {label}
      </label>
      <div className="flex-1">{children}</div>
    </div>
  )
}
