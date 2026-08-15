/**
 * 路由规则列表编辑器（step 级 routes 与循环体 exit_routes 复用）。
 *
 * 每条规则 = when 条件表达式 → then 动作（next 跳转目标 + set 状态写入）。
 * next 对齐内核 RouteNext：'loop' | 'end' | 'wait' | {step} | {phase}，
 * step/phase 目标从当前管道已知 id 下拉选择（也可保留未知现值）。
 * 空串 / 'True' 视为恒真（默认兜底路由）。
 */

import { ChevronDown, ChevronUp, Plus, Trash2 } from '@/assets/icons'
import { Input } from '@/components/ui/input'
import {
  buildRouteNext,
  parseRouteNext,
} from '@/services/pipeline/model'
import { KeyValueEditor } from './KeyValueEditor'
import type { Path, PipelineEditorOps, RouteRule } from '@/services/pipeline/model'

/** next 跳转类型选项（kind → 中文标签） */
const NEXT_KIND_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'loop', label: '继续循环 (loop)' },
  { value: 'end', label: '结束 (end)' },
  { value: 'wait', label: '挂起等待 (wait)' },
  { value: 'step', label: '跳转 step' },
  { value: 'phase', label: '转移到循环体 (phase)' },
]

const selectClass =
  'border-input bg-[var(--bg-input,hsl(var(--background)))] h-7 rounded-lg border px-2 text-xs focus:outline-none'

/**
 * 路由规则编辑器。
 *
 * @param rules 当前规则数组（可能 undefined）
 * @param arrayPath rules 数组在 raw data 中的路径
 * @param ops 编辑器操作集
 * @param knownStepIds 已知 step id（next=step 的目标选项）
 * @param knownPhaseIds 已知循环体 id（next=phase 的目标选项）
 * @param label 区块标题（如「路由分支」「循环体转移 exit_routes」）
 */
export function RouteRulesEditor({
  rules,
  arrayPath,
  ops,
  knownStepIds,
  knownPhaseIds,
  label,
}: {
  rules: RouteRule[] | undefined
  arrayPath: Path
  ops: PipelineEditorOps
  knownStepIds: string[]
  knownPhaseIds: string[]
  label: string
}) {
  const list = rules ?? []

  const addRule = () => {
    ops.insert(arrayPath, list.length, {
      when: 'True',
      then: { next: 'end' },
    })
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-foreground text-xs font-medium">{label}</span>
        <button
          type="button"
          onClick={addRule}
          className="text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)] flex items-center gap-1 rounded px-1.5 py-0.5 text-xs"
        >
          <Plus className="h-3.5 w-3.5" />
          添加规则
        </button>
      </div>

      {list.length === 0 && (
        <p className="text-muted-foreground text-xs">
          无路由规则（顺序执行后走默认转移语义）
        </p>
      )}

      {list.map((rule, i) => {
        const rulePath: Path = [...arrayPath, i]
        const next = parseRouteNext(rule?.then?.next)
        const targetIds = next.kind === 'phase' ? knownPhaseIds : knownStepIds
        const target = next.target ?? ''
        // 现值不在已知列表（如引用了公共库 step）也保留为选项，避免显示丢失
        const targetOptions =
          target && !targetIds.includes(target) ? [target, ...targetIds] : targetIds

        return (
          <div
            key={i}
            className="border-border bg-[var(--ds-bg-panel,rgba(148,163,184,0.04))] space-y-2 rounded-lg border p-2.5"
            data-testid={`route-rule-${i}`}
          >
            <div className="flex items-center gap-2">
              <span className="text-muted-foreground shrink-0 font-mono text-[10px]">
                #{i + 1}
              </span>
              <Input
                value={typeof rule?.when === 'string' ? rule.when : ''}
                onChange={(e) => ops.set([...rulePath, 'when'], e.target.value)}
                className="h-7 flex-1 font-mono text-xs"
                aria-label={`规则 ${i + 1} when 条件`}
                placeholder='条件表达式，如 "raw_tool_calls != []"，True=恒真'
                spellCheck={false}
              />
              <span className="text-muted-foreground shrink-0 text-[10px]">→</span>
              <select
                value={next.kind}
                onChange={(e) => {
                  const kind = e.target.value as typeof next.kind
                  ops.set(
                    [...rulePath, 'then', 'next'],
                    buildRouteNext({ kind, target: next.target }),
                  )
                }}
                className={selectClass}
                aria-label={`规则 ${i + 1} next 类型`}
              >
                {NEXT_KIND_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              {(next.kind === 'step' || next.kind === 'phase') && (
                <select
                  value={target}
                  onChange={(e) =>
                    ops.set(
                      [...rulePath, 'then', 'next'],
                      buildRouteNext({ kind: next.kind, target: e.target.value }),
                    )
                  }
                  className={selectClass}
                  aria-label={`规则 ${i + 1} next 目标`}
                >
                  <option value="">选择目标…</option>
                  {targetOptions.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              )}
              <div className="ml-auto flex shrink-0 items-center">
                <button
                  type="button"
                  onClick={() => ops.move(arrayPath, i, -1)}
                  disabled={i === 0}
                  className="text-muted-foreground hover:text-foreground rounded p-0.5 disabled:opacity-30"
                  aria-label={`规则 ${i + 1} 上移`}
                  title="上移"
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => ops.move(arrayPath, i, 1)}
                  disabled={i === list.length - 1}
                  className="text-muted-foreground hover:text-foreground rounded p-0.5 disabled:opacity-30"
                  aria-label={`规则 ${i + 1} 下移`}
                  title="下移"
                >
                  <ChevronDown className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => ops.remove(rulePath)}
                  className="text-muted-foreground hover:text-status-error rounded p-0.5"
                  aria-label={`规则 ${i + 1} 删除`}
                  title="删除规则"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            <div className="pl-6">
              <span className="text-muted-foreground mb-1 block text-[10px]">
                then.set（写入 state）
              </span>
              <KeyValueEditor
                value={rule?.then?.set}
                onChange={(nextSet) => {
                  if (nextSet === undefined) {
                    ops.remove([...rulePath, 'then', 'set'])
                  } else {
                    ops.set([...rulePath, 'then', 'set'], nextSet)
                  }
                }}
                emptyHint="不写入 state 字段"
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}
