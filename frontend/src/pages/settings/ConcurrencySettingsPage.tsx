/**
 * 并发控制配置页面
 *
 * 设置任务并发数、Agent 层级并发、LLM 并发、工作流并发、队列参数
 */

import { Loader2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  getConcurrencyConfig,
  saveConcurrencyConfig,
  type ConcurrencyConfigResponse,
  type TaskConcurrencyConfig,
  type AgentConcurrencyConfig,
  type WorkflowConcurrencyConfig,
  type LLMConcurrencyConfig,
} from '@/services/api/config'

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

const DEFAULT_CONFIG: ConcurrencyConfigResponse = {
  task: {
    max_concurrent_tasks: 5,
    task_max_workers: 10,
    task_timeout: 300,
  },
  agent: {
    l1_max_concurrent: 3,
    l2_max_concurrent: 5,
    l3_max_concurrent: 10,
  },
  workflow: {
    max_concurrent: 3,
  },
  llm: {
    zhipu_max_concurrent: 5,
    openai_max_concurrent: 5,
    anthropic_max_concurrent: 3,
    default_max_concurrent: 2,
  },
}

/** Agent 层级标签 */
const AGENT_LEVEL_LABELS: Record<string, string> = {
  l1_max_concurrent: 'L1 Agent (项目经理)',
  l2_max_concurrent: 'L2 Agent (团队负责人)',
  l3_max_concurrent: 'L3 Agent (执行者)',
}

/** LLM 提供商标签 */
const LLM_PROVIDER_LABELS: Record<string, string> = {
  zhipu_max_concurrent: '智谱 AI',
  openai_max_concurrent: 'OpenAI',
  anthropic_max_concurrent: 'Anthropic',
  default_max_concurrent: '默认提供商',
}

/**
 * 并发控制配置页面组件
 */
export function ConcurrencySettingsPage() {
  const [config, setConfig] = useState<ConcurrencyConfigResponse>(DEFAULT_CONFIG)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [activeTab, setActiveTab] = useState('task')

  // 加载配置
  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    getConcurrencyConfig()
      .then((data) => {
        if (!cancelled) setConfig(data)
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

  // 更新任务并发配置
  const updateTask = useCallback(<K extends keyof TaskConcurrencyConfig>(key: K, value: number) => {
    setConfig((prev) => ({
      ...prev,
      task: { ...prev.task, [key]: value },
    }))
  }, [])

  // 更新 Agent 并发配置
  const updateAgent = useCallback(
    <K extends keyof AgentConcurrencyConfig>(key: K, value: number) => {
      setConfig((prev) => ({
        ...prev,
        agent: { ...prev.agent, [key]: value },
      }))
    },
    [],
  )

  // 更新工作流并发配置
  const updateWorkflow = useCallback(
    <K extends keyof WorkflowConcurrencyConfig>(key: K, value: number) => {
      setConfig((prev) => ({
        ...prev,
        workflow: { ...prev.workflow, [key]: value },
      }))
    },
    [],
  )

  // 更新 LLM 并发配置
  const updateLlm = useCallback(<K extends keyof LLMConcurrencyConfig>(key: K, value: number) => {
    setConfig((prev) => ({
      ...prev,
      llm: { ...prev.llm, [key]: value },
    }))
  }, [])

  // 保存并发配置到后端
  const handleSave = useCallback(async () => {
    setSaveState('saving')
    try {
      const saved = await saveConcurrencyConfig(config)
      setConfig(saved)
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch {
      setSaveState('error')
    }
  }, [config])

  // 汇总并发数
  const totalMaxConcurrent =
    config.task.max_concurrent_tasks +
    config.agent.l1_max_concurrent +
    config.agent.l2_max_concurrent +
    config.agent.l3_max_concurrent

  if (isLoading) {
    return (
      <PageShell title="并发控制" description="设置任务并发数和队列参数">
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell title="并发控制" description="设置任务并发数和队列参数">
      {loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      {/* 概览卡片 */}
      <div className="mb-6 grid grid-cols-4 gap-3">
        <StatCard label="最大并发任务" value={config.task.max_concurrent_tasks} />
        <StatCard label="总 Agent 并发" value={totalMaxConcurrent} />
        <StatCard label="线程池" value={config.task.task_max_workers} />
        <StatCard label="工作流并发" value={config.workflow.max_concurrent} />
      </div>

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="task">任务并发</TabsTrigger>
          <TabsTrigger value="agent">Agent 层级</TabsTrigger>
          <TabsTrigger value="llm">LLM 并发</TabsTrigger>
          <TabsTrigger value="queue">队列管理</TabsTrigger>
        </TabsList>

        {/* 任务并发 */}
        <TabsContent value="task">
          <div className="mt-4 space-y-4">
            <FieldRow label="最大并发任务数" htmlFor="task-max">
              <NumberInput
                id="task-max"
                min={1}
                max={50}
                value={config.task.max_concurrent_tasks}
                onChange={(v) => updateTask('max_concurrent_tasks', v)}
                ariaLabel="最大并发任务数"
              />
              <p className="text-muted-foreground mt-1 text-xs">同时执行的最大任务数量</p>
            </FieldRow>
            <FieldRow label="线程池大小" htmlFor="task-workers">
              <NumberInput
                id="task-workers"
                min={1}
                max={100}
                value={config.task.task_max_workers}
                onChange={(v) => updateTask('task_max_workers', v)}
                ariaLabel="线程池大小"
              />
              <p className="text-muted-foreground mt-1 text-xs">任务执行线程池的工作线程数</p>
            </FieldRow>
            <FieldRow label="任务超时 (秒)" htmlFor="task-timeout">
              <NumberInput
                id="task-timeout"
                min={10}
                max={3600}
                value={config.task.task_timeout}
                onChange={(v) => updateTask('task_timeout', v)}
                ariaLabel="任务超时秒数"
              />
              <p className="text-muted-foreground mt-1 text-xs">单个任务的超时时间</p>
            </FieldRow>
          </div>
        </TabsContent>

        {/* Agent 层级并发 */}
        <TabsContent value="agent">
          <div className="mt-4 space-y-4">
            <p className="text-muted-foreground text-xs">
              不同层级 Agent 的最大并发数。L1 为项目经理，L2 为团队负责人，L3 为执行者。
            </p>
            {(Object.entries(AGENT_LEVEL_LABELS) as [keyof AgentConcurrencyConfig, string][]).map(
              ([key, label]) => (
                <FieldRow key={key} label={label} htmlFor={`agent-${key}`}>
                  <div className="flex items-center gap-3">
                    <input
                      type="range"
                      id={`agent-${key}`}
                      min={1}
                      max={20}
                      value={config.agent[key]}
                      onChange={(e) => updateAgent(key, Number(e.target.value))}
                      className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                    />
                    <span className="min-w-[30px] text-right font-mono text-sm">
                      {config.agent[key]}
                    </span>
                  </div>
                </FieldRow>
              ),
            )}
            {/* Agent 层级可视化 */}
            <div className="mt-4 border-t pt-3">
              <h3 className="text-muted-foreground mb-2 text-xs">并发层级结构</h3>
              <div className="space-y-1">
                {(['l1_max_concurrent', 'l2_max_conunct_concurrent', 'l3_max_concurrent'] as const)
                  .filter((k) => k in config.agent)
                  .map((key, idx) => {
                    const actualKey =
                      key === 'l2_max_conunct_concurrent' ? 'l2_max_concurrent' : key
                    const label = AGENT_LEVEL_LABELS[actualKey] || ''
                    const value = config.agent[actualKey as keyof AgentConcurrencyConfig]
                    return (
                      <div
                        key={actualKey}
                        className="flex items-center gap-2"
                        style={{ paddingLeft: `${idx * 24}px` }}
                      >
                        <div
                          className="bg-primary h-3 rounded-sm"
                          style={{ width: `${value * 16}px` }}
                        />
                        <span className="text-muted-foreground text-xs">
                          {label}: {value}
                        </span>
                      </div>
                    )
                  })}
              </div>
            </div>
          </div>
        </TabsContent>

        {/* LLM 并发 */}
        <TabsContent value="llm">
          <div className="mt-4 space-y-4">
            <p className="text-muted-foreground text-xs">
              各 LLM 提供商的 API 并发调用上限。超过此限制的请求将排队等待。
            </p>
            {(Object.entries(LLM_PROVIDER_LABELS) as [keyof LLMConcurrencyConfig, string][]).map(
              ([key, label]) => (
                <FieldRow key={key} label={label} htmlFor={`llm-${key}`}>
                  <NumberInput
                    id={`llm-${key}`}
                    min={1}
                    max={50}
                    value={config.llm[key]}
                    onChange={(v) => updateLlm(key, v)}
                    ariaLabel={`${label}并发数`}
                  />
                </FieldRow>
              ),
            )}
          </div>
        </TabsContent>

        {/* 队列管理 */}
        <TabsContent value="queue">
          <div className="mt-4 space-y-4">
            <p className="text-muted-foreground text-xs">
              队列管理设置。当并发数达到上限时，新请求将进入等待队列。
            </p>
            <div className="space-y-3">
              <QueueInfoRow label="任务队列" current={0} max={config.task.max_concurrent_tasks} />
              <QueueInfoRow
                label="L1 Agent 队列"
                current={0}
                max={config.agent.l1_max_concurrent}
              />
              <QueueInfoRow
                label="L2 Agent 队列"
                current={0}
                max={config.agent.l2_max_concurrent}
              />
              <QueueInfoRow
                label="L3 Agent 队列"
                current={0}
                max={config.agent.l3_max_concurrent}
              />
              <QueueInfoRow label="工作流队列" current={0} max={config.workflow.max_concurrent} />
            </div>
            <div className="bg-card text-muted-foreground mt-4 rounded-lg border px-3 py-2 text-xs">
              实时队列状态需要后端 WebSocket 推送支持，当前显示为静态视图。
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

/** 数字输入组件 */
function NumberInput({
  id,
  min,
  max,
  value,
  onChange,
  ariaLabel,
}: {
  id: string
  min: number
  max: number
  value: number
  onChange: (v: number) => void
  ariaLabel?: string
}) {
  return (
    <div className="flex items-center gap-2">
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-24"
        aria-label={ariaLabel}
      />
      <span className="text-muted-foreground text-xs">
        ({min} - {max})
      </span>
    </div>
  )
}

/** 统计卡片 */
function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-card rounded-lg border px-3 py-2 text-center">
      <div className="text-lg font-semibold">{value}</div>
      <div className="text-muted-foreground text-xs">{label}</div>
    </div>
  )
}

/** 队列信息行 */
function QueueInfoRow({ label, current, max }: { label: string; current: number; max: number }) {
  const usagePct = max > 0 ? (current / max) * 100 : 0
  return (
    <div className="bg-card flex items-center gap-3 rounded-lg border px-3 py-2">
      <span className="min-w-[120px] text-sm">{label}</span>
      <div className="bg-border h-2 flex-1 overflow-hidden rounded-full">
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{
            width: `${usagePct}%`,
            backgroundColor: usagePct > 80 ? '#ef4444' : usagePct > 50 ? '#f59e0b' : '#10b981',
          }}
        />
      </div>
      <span className="text-muted-foreground min-w-[60px] text-right text-xs">
        {current} / {max}
      </span>
    </div>
  )
}

/* ============================================ */
/* 共享子组件                                    */
/* ============================================ */

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
      <main className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label="并发控制配置表单">{children}</main>
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
