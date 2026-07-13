/** 上下文窗口配置页面 管理上下文窗口大小、记忆层级配置、压缩设置、Token 预算分配 */

import { Loader2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import {
  getContextWindowConfig,
  updateContextWindowConfig,
  resetContextWindowConfig,
} from '@/services/api/config'

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** 完整上下文配置（页面使用的本地类型，与后端 YAML 一一对应） 复用 ContextWindowConfig 的所有字段， budgets 仍以 Record 形式存储。 */
interface FullContextConfig {
  version: string
  compress_trigger_ratio: number
  budgets: Record<string, number>
  include_tools_description_in_prompt: boolean
  stability: Record<string, string>
  layer_order: string[]
  compression: {
    enabled: boolean
    model: string
    layer_trigger_ratio: number
    max_turn_ratio: number
  }
  custom_layers: Record<string, unknown>
}

/** 默认配置（与后端 _DEFAULT_CONTEXT_WINDOW 保持同步） */
const DEFAULT_FULL_CONFIG: FullContextConfig = {
  version: '2.0',
  compress_trigger_ratio: 0.55,
  budgets: {
    system_prompt: 0.06,
    tools_description: 0.0,
    static_vars: 0.03,
    dynamic_variables: 0.03,
    l3: 0.02,
    l2: 0.05,
    l1: 0.1,
    recent: 0.18,
    retrieval: 0.05,
    response_reserve: 0.14,
  },
  include_tools_description_in_prompt: false,
  stability: {},
  layer_order: [
    'system_prompt',
    'tools_description',
    'static_vars',
    'l3',
    'l2',
    'l1',
    'recent',
    'dynamic_variables',
  ],
  compression: {
    enabled: true,
    model: '',
    layer_trigger_ratio: 0.8,
    max_turn_ratio: 0.5,
  },
  custom_layers: {},
}

/** 层级中文标签 */
const LAYER_LABELS: Record<string, string> = {
  system_prompt: '系统提示词',
  tools_description: '工具描述',
  static_vars: '静态资源',
  dynamic_variables: '动态上下文',
  l3: 'L3 记忆 (关键词索引)',
  l2: 'L2 记忆 (摘要)',
  l1: 'L1 记忆 (详细历史)',
  recent: '最近消息',
  retrieval: '检索结果',
  response_reserve: '响应预留',
}

/** 上下文窗口配置页面组件 */
export function ContextWindowSettingsPage() {
  const [config, setConfig] = useState<FullContextConfig>(DEFAULT_FULL_CONFIG)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')

  // 加载配置
  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    getContextWindowConfig()
      .then((data) => {
        if (cancelled) return
        // 合并 API 返回的基础配置和默认完整配置
        setConfig({ ...DEFAULT_FULL_CONFIG, ...data })
      })
      .catch(() => {
        if (!cancelled) {
          setLoadError('无法连接服务器，显示默认配置')
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 更新预算
  const updateBudget = useCallback((layer: string, value: number) => {
    setConfig((prev) => ({
      ...prev,
      budgets: { ...prev.budgets, [layer]: value },
    }))
  }, [])

  // 更新压缩配置
  const updateCompression = useCallback(
    <K extends keyof FullContextConfig['compression']>(
      key: K,
      value: FullContextConfig['compression'][K],
    ) => {
      setConfig((prev) => ({
        ...prev,
        compression: { ...prev.compression, [key]: value },
      }))
    },
    [],
  )

  // 保存完整配置到后端
  const handleSave = useCallback(async () => {
    setSaveState('saving')
    try {
      const payload = {
        compress_trigger_ratio: config.compress_trigger_ratio,
        budgets: config.budgets,
        compression: config.compression,
        layer_order: config.layer_order,
      }
      const saved = await updateContextWindowConfig(payload)
      setConfig({ ...DEFAULT_FULL_CONFIG, ...saved })
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch {
      setSaveState('error')
    }
  }, [config])

  // 重置
  const handleReset = useCallback(async () => {
    setSaveState('saving')
    try {
      const saved = await resetContextWindowConfig()
      setConfig({ ...DEFAULT_FULL_CONFIG, ...saved })
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch {
      setSaveState('error')
    }
  }, [])

  // 计算总预算
  const totalBudget = Object.values(config.budgets).reduce((sum, v) => sum + v, 0)
  const budgetOk = Math.abs(totalBudget - 1.0) < 0.01

  if (isLoading) {
    return (
      <PageShell title="上下文窗口" description="管理上下文窗口大小和策略">
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell title="上下文窗口" description="管理上下文窗口大小和策略">
      {loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      {/* 记忆层级配置 */}
      <Section title="记忆层级">
        <p className="text-muted-foreground mb-3 text-xs">
          L1: 详细历史 (最近) / L2: 摘要 / L3: 关键词索引 (最远)
        </p>
        <div className="grid grid-cols-3 gap-3">
          {['l1', 'l2', 'l3'].map((level) => (
            <div key={level} className="bg-card rounded-lg border px-3 py-2 text-center">
              <div className="text-muted-foreground mb-1 text-xs">{LAYER_LABELS[level]}</div>
              <div className="text-lg font-semibold">{config.budgets[level] * 100}%</div>
              <Progress
                value={config.budgets[level] * 100}
                variant="default"
                className="mt-2 h-1.5"
              />
            </div>
          ))}
        </div>
      </Section>

      {/* Token 预算分配 */}
      <Section title="Token 预算分配">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-muted-foreground text-xs">
            总计: {(totalBudget * 100).toFixed(1)}%
          </span>
          <span className={`text-xs ${budgetOk ? 'text-status-success' : 'text-status-error'}`}>
            {budgetOk ? '总和 = 100%' : `偏差: ${((totalBudget - 1) * 100).toFixed(1)}%`}
          </span>
        </div>
        <div className="space-y-2">
          {config.layer_order.map((layer) => (
            <div key={layer} className="flex items-center gap-3">
              <span className="text-muted-foreground min-w-[120px] text-right text-xs">
                {LAYER_LABELS[layer] || layer}
              </span>
              <input
                type="range"
                min={0}
                max={0.5}
                step={0.01}
                value={config.budgets[layer] ?? 0}
                onChange={(e) => updateBudget(layer, Number(e.target.value))}
                className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
              />
              <span className="min-w-[50px] text-right font-mono text-xs">
                {((config.budgets[layer] ?? 0) * 100).toFixed(0)}%
              </span>
            </div>
          ))}
        </div>
        {/* 可视化条 */}
        <div className="mt-3 flex h-4 overflow-hidden rounded-full border">
          {config.layer_order.map((layer) => {
            const pct = (config.budgets[layer] ?? 0) * 100
            if (pct <= 0) return null
            return (
              <div
                key={layer}
                className="h-full transition-all duration-300"
                style={{
                  width: `${pct}%`,
                  backgroundColor: BUDGET_COLORS[layer] ?? '#6b7280',
                }}
                title={`${LAYER_LABELS[layer] || layer}: ${pct.toFixed(1)}%`}
              />
            )
          })}
        </div>
      </Section>

      {/* 压缩配置 */}
      <Section title="压缩设置">
        <FieldRow label="全局压缩触发比例" htmlFor="ctx-compress-trigger">
          <div className="flex items-center gap-3">
            <input
              type="range"
              id="ctx-compress-trigger"
              aria-label="全局压缩触发比例"
              min={0.5}
              max={0.6}
              step={0.01}
              value={config.compress_trigger_ratio}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  compress_trigger_ratio: Number(e.target.value),
                }))
              }
              className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
            />
            <span className="min-w-[40px] text-right text-sm">
              {config.compress_trigger_ratio}
            </span>
          </div>
          <span className="text-muted-foreground mt-1 text-xs">
            上下文占用达到此比例时触发压缩（0.55 = 55%）
          </span>
        </FieldRow>
        <FieldRow label="启用压缩" htmlFor="comp-enabled">
          <label className="flex cursor-pointer items-center gap-2">
            <input
              id="comp-enabled"
              type="checkbox"
              checked={config.compression.enabled}
              onChange={(e) => updateCompression('enabled', e.target.checked)}
              className="border-border rounded"
            />
            <span className="text-sm">启用自动压缩</span>
          </label>
        </FieldRow>
        <FieldRow label="单层触发比例" htmlFor="comp-trigger">
          <div className="flex items-center gap-3">
            <input
              type="range"
              id="comp-trigger"
              min={0.3}
              max={1}
              step={0.05}
              value={config.compression.layer_trigger_ratio}
              onChange={(e) => updateCompression('layer_trigger_ratio', Number(e.target.value))}
              className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
            />
            <span className="min-w-[40px] text-right text-sm">
              {config.compression.layer_trigger_ratio}
            </span>
          </div>
        </FieldRow>
        <FieldRow label="最大轮次比例" htmlFor="comp-turn">
          <div className="flex items-center gap-3">
            <input
              type="range"
              id="comp-turn"
              min={0.1}
              max={1}
              step={0.05}
              value={config.compression.max_turn_ratio}
              onChange={(e) => updateCompression('max_turn_ratio', Number(e.target.value))}
              className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
            />
            <span className="min-w-[40px] text-right text-sm">
              {config.compression.max_turn_ratio}
            </span>
          </div>
        </FieldRow>
      </Section>

      {/* 操作按钮 */}
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
        <Button variant="outline" onClick={handleReset} disabled={saveState === 'saving'}>
          {saveState === 'saving' ? (
            <>
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              重置中...
            </>
          ) : (
            '重置为默认'
          )}
        </Button>
        {saveState === 'saved' && (
          <span className="text-xs text-status-success" role="status">已保存</span>
        )}
        {saveState === 'error' && (
          <span className="text-xs text-status-error" role="alert">保存失败</span>
        )}
      </div>
    </PageShell>
  )
}

/** 预算颜色映射 */
const BUDGET_COLORS: Record<string, string> = {
  system_prompt: '#3b82f6',
  tools_description: '#8b5cf6',
  static_vars: '#6366f1',
  dynamic_variables: '#a855f7',
  l3: '#f59e0b',
  l2: '#f97316',
  l1: '#ef4444',
  recent: '#10b981',
  retrieval: '#06b6d4',
  response_reserve: '#ec4899',
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
      <main className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label="上下文窗口配置表单">{children}</main>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="text-foreground mb-3 text-sm font-semibold">{title}</h2>
      <div className="space-y-3">{children}</div>
    </section>
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
