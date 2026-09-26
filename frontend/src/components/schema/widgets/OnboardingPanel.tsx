/**
 * onboarding_panel — 引导清单页（VS Code Get Started 同构）。
 *
 * 内容来自 onboarding_service 插件（内容即数据，本组件是通用引擎）；
 * 完成判定 = 持久化进度 ∨ evaluator 纯函数求值（api_check 取数一次、
 * panel_visited 随工作区页签实时重算）。全绿 walkthrough 自动折叠。
 * 形态依据：docs/decisions/2026-09-24-onboarding-plugin.md（预置 widget 承载，
 * memory_panel 判据——交互复杂度超出 webview 沙箱能力）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { CheckCircle2 } from '@/assets/icons'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import apiClient from '@/services/api/client'
import {
  fetchOnboardingContent,
  fetchOnboardingProgress,
  postProgressUpdate,
} from '@/services/onboarding/content'
import { evaluateCondition, collectApiChecks } from '@/services/onboarding/evaluator'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { openWorkspacePanelByPath, TOP_NAV_PANELS } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { LlmSetupWizard } from './onboarding/LlmSetupWizard'
import type { EvaluationContext } from '@/services/onboarding/evaluator'
import type { OnboardingProgress, Walkthrough, WalkthroughStep } from '@/services/onboarding/types'
import type { ReactNode } from 'react'

/** 页签 id → 面板路径 反查表（panelPathToTabId 的逆口径，同源两段解析） */
function buildTabIdToPath(): Map<string, string> {
  const map = new Map<string, string>()
  for (const [path, spec] of Object.entries(TOP_NAV_PANELS)) {
    map.set(spec.id, path)
  }
  for (const page of contributionRegistry.getPages()) {
    if (page.path) map.set(`ws-plugin-${page.id}`, page.path)
  }
  return map
}

export function OnboardingPanel(): ReactNode {
  const [content, setContent] = useState<Walkthrough[] | null>(null)
  const [progress, setProgress] = useState<OnboardingProgress>({})
  const [loadError, setLoadError] = useState<string | null>(null)
  const [apiResults, setApiResults] = useState<Map<string, unknown>>(new Map())
  const [activeId, setActiveId] = useState<string | null>(null)
  const [wizardOpen, setWizardOpen] = useState(false)
  const workspaceTabs = useLayoutModeStore((s) => s.workspaceTabs)
  /** 已发起自动落账的步骤键（防 effect 重复 POST） */
  const autoMarkedRef = useRef(new Set<string>())

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchOnboardingContent(), fetchOnboardingProgress()])
      .then(([c, p]) => {
        if (cancelled) return
        setContent(c.walkthroughs)
        setProgress(p.progress)
        const first = c.walkthroughs.find((w) => w.default_open) ?? c.walkthroughs[0]
        if (first) setActiveId(first.id)
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError((e as Error).message || '引导内容加载失败')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const refreshApi = useCallback(async (): Promise<void> => {
    if (!content) return
    const endpoints = [
      ...new Set(
        content.flatMap((w) =>
          w.steps.flatMap((s) => (s.completion ? collectApiChecks(s.completion) : [])),
        ),
      ),
    ].map((c) => c.endpoint)
    const results = await Promise.all(
      endpoints.map(async (endpoint) => {
        try {
          const res = await apiClient.get<unknown>(endpoint)
          return [endpoint, res.data] as const
        } catch {
          return [endpoint, undefined] as const
        }
      }),
    )
    setApiResults(new Map(results))
  }, [content])

  useEffect(() => {
    void refreshApi()
  }, [refreshApi])

  const visitedPanels = useMemo(() => {
    const tabIdToPath = buildTabIdToPath()
    const paths = workspaceTabs
      .map((t) => tabIdToPath.get(t.id))
      .filter((p): p is string => !!p)
    return new Set(paths)
  }, [workspaceTabs])

  const ctx: EvaluationContext = useMemo(
    () => ({
      apiResults,
      visitedPanels,
      selectedModes: new Set<string>(),
      ctaClickedSteps: new Set<string>(),
    }),
    [apiResults, visitedPanels],
  )

  const persist = useCallback(
    async (update: Parameters<typeof postProgressUpdate>[0]): Promise<void> => {
      try {
        const res = await postProgressUpdate(update)
        setProgress(res.progress)
      } catch {
        // 进度落账失败不打断浏览（下次进入重算自动条件仍会补账）
      }
    },
    [],
  )

  // 自动落账：条件型步骤首次满足即记进度（how = 条件类型），manual 不自动
  useEffect(() => {
    if (!content) return
    for (const w of content) {
      for (const step of w.steps) {
        if (!step.completion || step.completion.type === 'manual') continue
        const key = `${w.id}/${step.id}`
        if (progress[w.id]?.[step.id]?.done === true || autoMarkedRef.current.has(key)) continue
        if (evaluateCondition(step.completion, ctx, key)) {
          autoMarkedRef.current.add(key)
          void persist({
            walkthrough_id: w.id,
            step_id: step.id,
            done: true,
            how: step.completion.type,
          })
        }
      }
    }
  }, [content, ctx, progress, persist])

  if (loadError) {
    return <ErrorState message={loadError} onRetry={() => window.location.reload()} />
  }
  if (!content) return <LoadingState />
  if (content.length === 0) {
    return <div className="text-muted-foreground p-8 text-sm">暂无引导内容。</div>
  }

  const active = content.find((w) => w.id === activeId) ?? content[0]

  const stepDone = (w: Walkthrough, step: WalkthroughStep): boolean =>
    progress[w.id]?.[step.id]?.done === true

  const stepSatisfied = (w: Walkthrough, step: WalkthroughStep): boolean =>
    !!step.completion &&
    step.completion.type !== 'manual' &&
    evaluateCondition(step.completion, ctx, `${w.id}/${step.id}`)

  const effectiveDone = (w: Walkthrough, step: WalkthroughStep): boolean =>
    stepDone(w, step) || stepSatisfied(w, step)

  const doneCount = (w: Walkthrough): number => w.steps.filter((s) => effectiveDone(w, s)).length

  const dispatchCta = (step: WalkthroughStep): void => {
    const action = step.cta?.action
    if (!action) return
    if (action.type === 'open_panel' && action.target) {
      openWorkspacePanelByPath(action.target)
    } else if (action.type === 'external_url' && action.target) {
      window.open(action.target, '_blank', 'noopener')
    }
  }

  return (
    <div className="flex h-full min-h-0" data-testid="onboarding-panel">
      {/* 左列：walkthrough 清单 + 进度 */}
      <aside className="border-border w-64 shrink-0 overflow-y-auto border-r p-3">
        <div className="text-foreground mb-2 px-1 text-sm font-semibold">使用引导</div>
        {content.map((w) => {
          const done = doneCount(w)
          const all = w.steps.length
          return (
            <button
              key={w.id}
              type="button"
              onClick={() => setActiveId(w.id)}
              className={cn(
                'mb-1 w-full rounded-md px-2 py-2 text-left transition-colors',
                w.id === active.id
                  ? 'bg-accent text-foreground'
                  : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium">{w.title}</span>
                <span
                  className={cn(
                    'shrink-0 font-mono text-[10px]',
                    done === all ? 'text-green-600' : 'text-muted-foreground',
                  )}
                  data-testid={`progress-${w.id}`}
                >
                  {done === all ? '✓ ' : ''}
                  {done}/{all}
                </span>
              </div>
              <div className="bg-border mt-1.5 h-1 w-full overflow-hidden rounded-full">
                <div
                  className="bg-primary h-full rounded-full transition-all"
                  style={{ width: `${all === 0 ? 0 : (done / all) * 100}%` }}
                />
              </div>
            </button>
          )
        })}
      </aside>

      {/* 右列：当前 walkthrough 步骤卡 */}
      <main className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-2xl">
          <h2 className="text-foreground text-xl font-semibold">{active.title}</h2>
          <p className="text-muted-foreground mt-1 text-sm">{active.description}</p>
          <div className="mt-5 flex flex-col gap-4">
            {active.steps.map((step, idx) => {
              const done = effectiveDone(active, step)
              return (
                <div
                  key={step.id}
                  className={cn(
                    'border-border bg-card rounded-xl border p-4',
                    done && 'opacity-75',
                  )}
                  data-testid={`step-${active.id}-${step.id}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <span className="text-muted-foreground font-mono text-xs">{idx + 1}</span>
                      <span className="text-foreground font-medium">{step.title}</span>
                    </div>
                    {done && (
                      <span className="text-muted-foreground flex shrink-0 items-center gap-1 text-xs">
                        <CheckCircle2 className="h-4 w-4 text-green-600" />
                        已完成
                      </span>
                    )}
                  </div>
                  <div className="text-muted-foreground prose prose-sm mt-2 max-w-none text-sm">
                    <ReactMarkdown>{step.body}</ReactMarkdown>
                  </div>
                  {step.wizard === 'llm_setup' && !done && (
                    <div className="mt-3">
                      {wizardOpen ? (
                        <LlmSetupWizard
                          onConfigured={() => void refreshApi()}
                          onOpenAdvanced={() => openWorkspacePanelByPath('/settings')}
                        />
                      ) : (
                        <Button size="sm" onClick={() => setWizardOpen(true)}>
                          打开配置向导
                        </Button>
                      )}
                    </div>
                  )}
                  {step.cta && (
                    <div className="mt-3">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={step.cta.action.type !== 'open_panel' && step.cta.action.type !== 'external_url'}
                        onClick={() => dispatchCta(step)}
                      >
                        {step.cta.label}
                      </Button>
                    </div>
                  )}
                  {!done && step.completion?.type === 'manual' && (
                    <div className="mt-3">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          void persist({
                            walkthrough_id: active.id,
                            step_id: step.id,
                            done: true,
                            how: 'manual',
                          })
                        }
                      >
                        标记完成
                      </Button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </main>
    </div>
  )
}

export default OnboardingPanel
