/**
 * 路由规则列表编辑器（step 级 next 与循环体级 next 复用）。
 *
 * 每条规则 = G10 文件 DSL 转移分支：`{when?, then: 目标字符串, set?}`。
 * then 目标选项按 scope 收窄（内核加载期校验口径，非法目标启动报错）：
 * - step 级：end / loop / 本循环体 step id / 循环体 id（跨体转移）；
 * - 循环体级：end / 循环体 id（loop 非法——出口转移在体循环结束后求值）。
 * `wait` 已退役（挂起由 state.suspended 表达），不提供。
 */

import { ChevronDown, ChevronUp, Plus, Trash2 } from '@/assets/icons'
import { Input } from '@/components/ui/input'
import { KeyValueEditor } from './KeyValueEditor'
import type { Path, PipelineEditorOps, TransitionRule } from '@/services/pipeline/model'

/** 目标下拉选项 */
interface TargetOption {
  value: string
  label: string
}

/** 构建目标选项；现值不在合法集（如历史遗留目标）也保留，避免显示丢失 */
function buildTargetOptions(
  scope: 'step' | 'body',
  localStepIds: string[],
  knownBodyIds: string[],
  current: string,
): TargetOption[] {
  const options: TargetOption[] =
    scope === 'step'
      ? [
          { value: 'end', label: '结束 (end)' },
          { value: 'loop', label: '继续循环 (loop)' },
          ...localStepIds.map((id) => ({ value: id, label: `step: ${id}` })),
          ...knownBodyIds.map((id) => ({ value: id, label: `体: ${id}` })),
        ]
      : [
          { value: 'end', label: '结束 (end)' },
          ...knownBodyIds.map((id) => ({ value: id, label: `体: ${id}` })),
        ]
  if (current && !options.some((o) => o.value === current)) {
    return [{ value: current, label: current }, ...options]
  }
  return options
}

const selectClass =
  'border-input bg-[var(--bg-input,hsl(var(--background)))] h-7 rounded-lg border px-2 text-xs focus:outline-none'

/**
 * 路由规则编辑器（G10 DSL：when 条件 → then 目标字符串 + set 状态写入）。
 *
 * @param rules 当前规则数组（可能 undefined）
 * @param arrayPath rules 数组在 raw data 中的路径（step 的 `next` / 循环体的 `next`）
 * @param ops 编辑器操作集
 * @param scope 规则挂载层级（决定 then 目标合法集）
 * @param localStepIds step 级专用：本循环体 step id 集（then 本地跳转目标）
 * @param knownBodyIds 全部循环体 id（step 级跨体目标 / 循环体级转移目标）
 * @param label 区块标题
 */
export function RouteRulesEditor({
  rules,
  arrayPath,
  ops,
  scope,
  localStepIds = [],
  knownBodyIds,
  label,
}: {
  rules: TransitionRule[] | undefined
  arrayPath: Path
  ops: PipelineEditorOps
  scope: 'step' | 'body'
  localStepIds?: string[]
  knownBodyIds: string[]
  label: string
}) {
  const list = rules ?? []

  const addRule = () => {
    ops.insert(arrayPath, list.length, { when: 'True', then: 'end' })
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
        const target = typeof rule?.then === 'string' ? rule.then : ''
        const targetOptions = buildTargetOptions(scope, localStepIds, knownBodyIds, target)

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
                onChange={(e) => {
                  const v = e.target.value
                  if (v === '') {
                    // 空条件 = 恒真，摘掉键（缺省语义），不写空串
                    ops.remove([...rulePath, 'when'])
                  } else {
                    ops.set([...rulePath, 'when'], v)
                  }
                }}
                className="h-7 flex-1 font-mono text-xs"
                aria-label={`规则 ${i + 1} when 条件`}
                placeholder='条件表达式，如 "raw_tool_calls != []"，空 = 恒真'
                spellCheck={false}
              />
              <span className="text-muted-foreground shrink-0 text-[10px]">→</span>
              <select
                value={target}
                onChange={(e) => ops.set([...rulePath, 'then'], e.target.value)}
                className={selectClass}
                aria-label={`规则 ${i + 1} then 目标`}
              >
                {target === '' && <option value="">选择目标…</option>}
                {targetOptions.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
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
                set（写入 state）
              </span>
              <KeyValueEditor
                value={rule?.set}
                onChange={(nextSet) => {
                  if (nextSet === undefined) {
                    ops.remove([...rulePath, 'set'])
                  } else {
                    ops.set([...rulePath, 'set'], nextSet)
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
