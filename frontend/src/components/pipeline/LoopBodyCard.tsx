/**
 * 循环体卡片（0.2 多循环体管道的一个执行体）。
 *
 * 头部：id + 循环/单次徽标 + 迭代上限 + run_on_error「错误必经」徽标；
 * 可折叠体设置（loop_config / run_on_error / exit_routes）；
 * 主体：step 节点列表（增删/排序）。
 */

import { useState } from 'react'
import { Plus, RotateCcw, ShieldCheck } from '@/assets/icons'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { RouteRulesEditor } from './RouteRulesEditor'
import { StepNode } from './StepNode'
import type { PipelinePluginCatalogEntry } from '@/services/api/pipelines'
import type { LoopBodyV2, Path, PipelineEditorOps } from '@/services/pipeline/model'

/**
 * 循环体卡片。
 *
 * @param body raw data 中该循环体的类型视图
 * @param bodyPath 该体在 raw data 中的路径（如 ['loop_bodies', 1]）
 * @param bodyIndex 体下标（展示序号）
 * @param ops 编辑器操作集
 * @param catalog 插件目录（透传 StepNode）
 * @param knownStepIds 管道内全部 step id
 * @param knownPhaseIds 全部循环体 id
 * @param knownStepIdSet step id 集合（新 step 去重命名）
 */
export function LoopBodyCard({
  body,
  bodyPath,
  bodyIndex,
  ops,
  catalog,
  knownStepIds,
  knownPhaseIds,
  knownStepIdSet,
}: {
  body: LoopBodyV2
  bodyPath: Path
  bodyIndex: number
  ops: PipelineEditorOps
  catalog: PipelinePluginCatalogEntry[]
  knownStepIds: string[]
  knownPhaseIds: string[]
  knownStepIdSet: Set<string>
}) {
  const [settingsOpen, setSettingsOpen] = useState(false)
  const steps = Array.isArray(body?.steps) ? body.steps : []
  const stepsPath: Path = [...bodyPath, 'steps']
  const looping = body?.loop_config?.enabled === true

  const addStep = () => {
    let id = 'new_step'
    for (let i = 1; knownStepIdSet.has(id); i++) id = `new_step_${i}`
    ops.insert(stepsPath, steps.length, { id, steps: [] })
  }

  return (
    <section
      className="border-border bg-[var(--ds-bg-elevated,#111C38)] rounded-2xl border p-4"
      data-testid={`loop-body-${body?.id ?? bodyIndex}`}
    >
      {/* 体头部 */}
      <header className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground font-mono text-[10px]">
          #{bodyIndex + 1}
        </span>
        <Input
          value={typeof body?.id === 'string' ? body.id : ''}
          onChange={(e) => ops.set([...bodyPath, 'id'], e.target.value)}
          className="h-7 w-36 border-transparent bg-transparent px-1 text-sm font-semibold"
          aria-label="循环体 id"
          spellCheck={false}
        />
        {looping ? (
          <Badge variant="info" className="gap-1 text-[10px]">
            <RotateCcw className="h-3 w-3" />
            循环体
            {typeof body.loop_config?.max_iterations === 'number' && (
              <span className="font-mono">
                {body.loop_config.max_iterations === -1
                  ? ' ∞'
                  : ` ×${body.loop_config.max_iterations}`}
              </span>
            )}
          </Badge>
        ) : (
          <Badge variant="secondary" className="text-[10px]">
            单次执行
          </Badge>
        )}
        {body?.run_on_error && (
          <Badge variant="warning" className="gap-1 text-[10px]" title="提前终止（ended/出错）时仍执行收尾">
            <ShieldCheck className="h-3 w-3" />
            错误必经
          </Badge>
        )}
        <button
          type="button"
          onClick={() => setSettingsOpen((v) => !v)}
          className="text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)] ml-auto rounded px-2 py-1 text-xs"
          aria-expanded={settingsOpen}
        >
          体设置{settingsOpen ? ' ▲' : ' ▼'}
        </button>
      </header>

      {/* 体设置：loop_config / run_on_error / exit_routes */}
      {settingsOpen && (
        <div className="border-border bg-[var(--ds-bg-panel,rgba(148,163,184,0.04))] mb-3 space-y-3 rounded-xl border p-3">
          <div className="text-muted-foreground flex flex-wrap items-center gap-4 text-xs">
            <label className="flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={looping}
                onChange={(e) =>
                  ops.set([...bodyPath, 'loop_config', 'enabled'], e.target.checked)
                }
                className="h-3.5 w-3.5 accent-[var(--btn-primary-bg)]"
              />
              启用循环
            </label>
            <label className="flex items-center gap-1.5">
              最大迭代
              <Input
                type="number"
                value={
                  typeof body?.loop_config?.max_iterations === 'number'
                    ? body.loop_config.max_iterations
                    : ''
                }
                onChange={(e) => {
                  const parsed = Number(e.target.value)
                  ops.set(
                    [...bodyPath, 'loop_config', 'max_iterations'],
                    e.target.value === '' || Number.isNaN(parsed)
                      ? e.target.value
                      : parsed,
                  )
                }}
                className="h-7 w-24 font-mono text-xs"
                aria-label="循环体最大迭代次数"
              />
            </label>
            <label className="flex cursor-pointer items-center gap-1.5" title="ended/出错提前终止时仍执行本收尾体">
              <input
                type="checkbox"
                checked={body?.run_on_error === true}
                onChange={(e) => ops.set([...bodyPath, 'run_on_error'], e.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--btn-primary-bg)]"
              />
              run_on_error（错误必经）
            </label>
          </div>
          <RouteRulesEditor
            rules={body?.exit_routes}
            arrayPath={[...bodyPath, 'exit_routes']}
            ops={ops}
            knownStepIds={knownStepIds}
            knownPhaseIds={knownPhaseIds}
            label="exit_routes（循环体结束转移；不配 = 默认顺序进下一个体）"
          />
        </div>
      )}

      {/* step 节点列表 */}
      <div className="space-y-2.5">
        {steps.map((step, i) => (
          <StepNode
            key={`${step?.id ?? i}-${i}`}
            step={step}
            stepPath={[...stepsPath, i]}
            stepIndex={i}
            totalSteps={steps.length}
            ops={ops}
            catalog={catalog}
            knownStepIds={knownStepIds}
            knownPhaseIds={knownPhaseIds}
          />
        ))}
        {steps.length === 0 && (
          <p className="text-muted-foreground py-4 text-center text-xs">
            空循环体（不会执行任何 step）
          </p>
        )}
        <button
          type="button"
          onClick={addStep}
          className="text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)] flex w-full items-center justify-center gap-1 rounded-xl border border-dashed py-2 text-xs"
          style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
        >
          <Plus className="h-3.5 w-3.5" />
          添加 step
        </button>
      </div>
    </section>
  )
}
