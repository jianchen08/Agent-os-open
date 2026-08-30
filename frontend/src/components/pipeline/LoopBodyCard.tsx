/**
 * 循环体卡片（0.2 多循环体管道的一个执行体）。
 *
 * 头部：id + 循环/单次徽标（按 G10 `while` 判定）+ run_on_error「错误必经」徽标；
 * 可折叠体设置（while / run_on_error / next 出口转移）；
 * 主体：step 节点列表（增删/排序）。
 */

import { useState } from 'react'
import { Plus, RotateCcw, ShieldCheck } from '@/assets/icons'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { PipeHooksDisplay } from './PipeHooksDisplay'
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
 * @param knownStepIds 管道内全部 step id（透传 StepNode 引用分类）
 * @param knownPhaseIds 全部循环体 id（体级 next 转移目标）
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
  // G10：体级循环由 `while` 表达（缺省 = 单次执行；迭代上限由 stop_check 兜底）
  const looping = typeof body?.while === 'string' && body.while.trim() !== ''
  // 本体 step id 集：step 级 next 的本地跳转目标（内核只接受同体内 step 目标）
  const localStepIds = steps
    .map((s) => (typeof s?.id === 'string' ? s.id : ''))
    .filter((id) => id !== '')

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
          <Badge
            variant="info"
            className="gap-1 text-[10px]"
            title={`循环继续条件 while: ${body.while}`}
          >
            <RotateCcw className="h-3 w-3" />
            循环体
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

      {/* 体设置：while / run_on_error / next */}
      {settingsOpen && (
        <div className="border-border bg-[var(--ds-bg-panel,rgba(148,163,184,0.04))] mb-3 space-y-3 rounded-xl border p-3">
          <div className="text-muted-foreground flex flex-wrap items-center gap-4 text-xs">
            <label className="flex items-center gap-1.5">
              while（循环继续条件）
              <Input
                value={typeof body?.while === 'string' ? body.while : ''}
                onChange={(e) => {
                  const v = e.target.value
                  if (v.trim() === '') {
                    // 空条件 = 单次执行，摘掉键（缺省语义）
                    ops.remove([...bodyPath, 'while'])
                  } else {
                    ops.set([...bodyPath, 'while'], v)
                  }
                }}
                placeholder='如 "True"（恒真循环）；空 = 单次执行'
                className="h-7 w-56 font-mono text-xs"
                aria-label="循环体 while 条件"
                spellCheck={false}
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
            rules={body?.next}
            arrayPath={[...bodyPath, 'next']}
            ops={ops}
            scope="body"
            knownBodyIds={knownPhaseIds}
            label="next（循环体结束转移；不配 = 默认顺序进下一个体）"
          />
          <section>
            <h4 className="text-foreground mb-1.5 text-xs font-medium">
              hooks（体级钩子，只读）
            </h4>
            <PipeHooksDisplay hooks={body?.hooks} scopeHint={`body:${body?.id ?? bodyIndex}`} />
          </section>
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
            bodyStepIds={localStepIds}
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
